"""Parser for Claude plugins (plugin.json format).

Aligns with the Claude Code plugin spec:
  https://docs.anthropic.com/en/docs/claude-code/plugins

Key spec rules:
- The manifest (.claude-plugin/plugin.json) is **optional**.
- When present, only `name` is required; everything else is optional metadata.
- When absent, the plugin name is derived from the directory name.
- Standard component directories: agents/, commands/, skills/, hooks/
- Pass-through files: .mcp.json, .lsp.json, settings.json
"""

import json
import logging
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import yaml

from ..utils.atomic_io import atomic_write_text, write_text_lf
from ..utils.console import _rich_warning
from ..utils.path_security import PathTraversalError, ensure_path_within

_logger = logging.getLogger(__name__)

# Untrusted plugin-package JSON files (.mcp.json / .lsp.json / plugin.json) are
# read straight off attacker-controlled package content during ``apm install``.
# Stock ``json.load`` has two failure classes that escape a narrow
# ``except json.JSONDecodeError``: a deeply nested document raises
# ``RecursionError`` and a >4300-digit integer literal raises ``ValueError``
# (the stdlib int-string conversion limit) -- either crashes a default command.
# Cap the file size first, then funnel every parse failure into a single
# ``ValueError`` so callers fail closed with one except type.
_MAX_PLUGIN_JSON_BYTES = 5 * 1024 * 1024
_LSP_FIELD_ALIASES = (
    ("extensionToLanguage", "fileExtensions"),
    ("startupTimeout", "warmupTimeoutMs"),
)
_PLUGIN_SKILL_SOURCES_FILE = ".plugin-skill-sources.json"


def _bounded_read_json(path: Path) -> Any:
    """Read and JSON-parse a plugin-package file fail-closed under a size cap.

    Raises ``ValueError`` on any parse failure (oversize, malformed, deep-nest
    ``RecursionError``, huge-int / ``MemoryError``). ``OSError`` (missing or
    unreadable file) propagates unchanged so callers can distinguish it.
    """
    size = path.stat().st_size
    if size > _MAX_PLUGIN_JSON_BYTES:
        raise ValueError(
            f"JSON file {path} exceeds {_MAX_PLUGIN_JSON_BYTES}-byte cap ({size} bytes)"
        )
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except (ValueError, RecursionError, MemoryError) as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


class PluginIntegrityError(RuntimeError):
    """Raised when a plugin destination tree contains a pre-existing symlink.

    Refusing to copy through a symlinked destination is defense-in-depth
    for the data-loss-adjacent ``shutil.copytree(..., dirs_exist_ok=True)``
    flow in ``_map_plugin_artifacts``. A malicious package shipping
    ``.apm/skills/<name>`` (or any other target_* subtree) as a symlink to
    an external path (e.g. ``/etc``, ``$HOME/.ssh``) would otherwise
    redirect writes outside the plugin root.
    """


class DeclaredPluginComponentError(PluginIntegrityError):
    """Raised when a plugin explicitly declares an unsatisfied component path."""


def _assert_no_symlink_descendants(target: Path) -> None:
    """Refuse to copy when *target* or any of its descendants is a symlink.

    Uses ``lstat``/``os.walk(followlinks=False)`` so the check itself does
    not traverse a hostile symlink. No-op when *target* does not exist.
    """
    if not target.exists() and not target.is_symlink():
        return
    if target.is_symlink():
        raise PluginIntegrityError(f"Refusing to copy into symlinked plugin destination: {target}")
    for root, dirs, files in os.walk(target, followlinks=False):
        root_path = Path(root)
        for name in dirs + files:
            entry = root_path / name
            if entry.is_symlink():
                raise PluginIntegrityError(
                    f"Refusing to copy into plugin destination containing symlinked entry: {entry}"
                )


def _surface_warning(message: str, logger: logging.Logger) -> None:
    """Emit one standard user-facing warning, with logging as fallback."""
    try:
        _rich_warning(message, symbol="warning")
    except Exception:
        logger.warning(message)


def _is_within_plugin(candidate: Path, plugin_root: Path, *, component: str) -> bool:
    """Return True iff *candidate* resolves inside *plugin_root*.

    Logs a warning and returns False when the path escapes the plugin
    root (absolute path, ``..`` traversal, or symlink pointing outside).
    Used to enforce the trust boundary on attacker-controlled manifest
    fields (agents/skills/commands/hooks) during plugin normalization.

    The rejected path string and resolved exception are deliberately
    omitted from log output: manifest values are externally controlled
    and static-analysis tooling treats them as tainted/sensitive. The
    component name alone is sufficient to identify which manifest field
    was rejected; operators that need the full value can reproduce
    locally with a clean checkout.
    """
    try:
        ensure_path_within(candidate, plugin_root)
    except PathTraversalError:
        _logger.warning(
            "Skipping %s entry: path escapes plugin root",
            component,
        )
        return False
    return True


def _has_symlink_component(candidate: Path, plugin_root: Path) -> bool:
    """Return True when a candidate reaches its target through a symlink."""
    try:
        relative = candidate.relative_to(plugin_root)
    except ValueError:
        return True
    current = plugin_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _iter_command_source_files(
    source: Path,
    target_root: Path,
    staging_root: Path,
    staging_parent: Path,
    ignore_non_content: Callable[[str, list[str]], set[str]],
) -> Iterator[tuple[Path, Path]]:
    """Yield regular command files while pruning generated staging output."""
    try:
        source_root = source.resolve()
    except OSError:
        _surface_warning("Skipping command source with an unresolvable staging boundary", _logger)
        return

    if source_root == target_root:
        return
    if source_root == staging_parent:
        excluded_root = staging_root
    else:
        try:
            target_root.relative_to(source_root)
        except ValueError:
            excluded_root = None
        else:
            excluded_root = target_root

    for directory, directories, files in os.walk(source_root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        if excluded_root is not None and directory_path == excluded_root.parent:
            directories[:] = [name for name in directories if name != excluded_root.name]
        ignored = ignore_non_content(directory, files)
        for name in files:
            source_file = directory_path / name
            if name not in ignored and not source_file.is_symlink() and source_file.is_file():
                yield source_file, source_root


def _holds_skill_dirs(candidate: Path) -> bool:
    """Return True iff *candidate* is a container of skills, not a skill itself.

    A directory carrying its own ``SKILL.md`` is the skill, however deep it
    sits in the plugin (``skills/engineering/tdd``). Anything else that has at
    least one immediate child carrying ``SKILL.md`` is a container holding
    them (the conventional ``skills/``). Directories matching neither shape
    are not containers, so a declared entry keeps its own name rather than
    spilling unrecognized contents into the shared skills root.
    """
    if (candidate / "SKILL.md").is_file() and not (candidate / "SKILL.md").is_symlink():
        return False
    try:
        return any(
            (child / "SKILL.md").is_file()
            and not child.is_symlink()
            and not (child / "SKILL.md").is_symlink()
            for child in candidate.iterdir()
            if child.is_dir() and not child.is_symlink()
        )
    except OSError:
        return False


def _immediate_skill_dirs(candidate: Path) -> list[Path]:
    """Return direct non-symlink skill children in a declared container."""
    try:
        return sorted(
            (
                child
                for child in candidate.iterdir()
                if child.is_dir()
                and not child.is_symlink()
                and (child / "SKILL.md").is_file()
                and not (child / "SKILL.md").is_symlink()
            ),
            key=lambda child: child.name,
        )
    except OSError:
        return []


def _map_plugin_skills(
    plugin_path: Path,
    apm_dir: Path,
    manifest: dict[str, Any],
    resolve_sources: Callable[[str, str], list[Path]],
    is_same_path: Callable[[Path, Path], bool],
    ignore_non_content: Callable[..., list[str]],
) -> None:
    """Materialize parser-authorized plugin skills and persist their receipt."""
    skill_sources = resolve_sources("skills", "skills")
    declared_skills = isinstance(manifest.get("skills"), (list, str))
    normalized_skill_sources: dict[str, Path] = {}
    duplicate_skill_names: set[str] = set()

    def add_normalized_skill_source(name: str, source: Path) -> None:
        """Keep ambiguous declared leaf names out of the deployable receipt."""
        if name in duplicate_skill_names:
            return
        existing = normalized_skill_sources.get(name)
        if existing is not None and existing != source:
            duplicate_skill_names.add(name)
            normalized_skill_sources.pop(name)
            return
        normalized_skill_sources[name] = source

    target_skills = apm_dir / "skills"
    _assert_no_symlink_descendants(target_skills)
    skill_dirs = [source for source in skill_sources if source.is_dir()]
    skill_files = [source for source in skill_sources if source.is_file()]

    for directory in skill_dirs:
        if declared_skills and not _holds_skill_dirs(directory):
            if (directory / "SKILL.md").is_file() and not (directory / "SKILL.md").is_symlink():
                add_normalized_skill_source(directory.name, directory)
            continue
        for child in _immediate_skill_dirs(directory):
            add_normalized_skill_source(child.name, child)

    # A declaration may change from a container to a file-only entry. Remove
    # the parser-owned former entries before handling either source shape.
    _remove_stale_normalized_skills(plugin_path, target_skills, normalized_skill_sources)

    if skill_dirs or skill_files:
        target_skills.mkdir(parents=True, exist_ok=True)
    for directory in skill_dirs:
        if declared_skills and not _holds_skill_dirs(directory):
            dest = target_skills / directory.name
            if not (directory / "SKILL.md").is_file():
                _warn_skills_entry_holds_no_skill(directory, plugin_path)
        else:
            dest = target_skills
        if is_same_path(directory, dest):
            continue
        shutil.copytree(directory, dest, dirs_exist_ok=True, ignore=ignore_non_content)
    for source_file in skill_files:
        destination = target_skills / source_file.name
        if is_same_path(source_file, destination):
            continue
        shutil.copy2(source_file, destination)

    _write_plugin_skill_sources(
        plugin_path,
        apm_dir,
        normalized_skill_sources,
        declared=declared_skills,
    )


def _read_plugin_skill_sources(plugin_path: Path) -> tuple[dict[str, str], bool]:
    """Read parser-owned normalized skill sources without trusting their paths."""
    receipt = plugin_path / ".apm" / _PLUGIN_SKILL_SOURCES_FILE
    if not receipt.is_file() or receipt.is_symlink():
        return {}, False
    try:
        data = _bounded_read_json(receipt)
    except (OSError, ValueError):
        return {}, False
    if not isinstance(data, dict) or data.get("version") != 1:
        return {}, False
    raw_sources = data.get("sources")
    if not isinstance(raw_sources, dict):
        return {}, False
    sources = {
        name: source
        for name, source in raw_sources.items()
        if isinstance(name, str)
        and name
        and Path(name).name == name
        and isinstance(source, str)
        and source
    }
    return sources, data.get("declared") is True


def normalized_plugin_skill_sources(plugin_path: Path) -> tuple[dict[str, Path], bool]:
    """Return parser-authorized normalized skill names and their byte sources.

    The receipt is written only by ``_map_plugin_artifacts`` after it validates
    declared plugin paths and materializes the matching flat ``.apm/skills``
    entries. Consumers must not recover membership by scanning raw ``skills/``.
    """
    plugin_path = plugin_path.resolve()
    raw_sources, declared = _read_plugin_skill_sources(plugin_path)
    normalized_root = plugin_path / ".apm" / "skills"
    resolved: dict[str, Path] = {}
    for name, relative_source in raw_sources.items():
        normalized = normalized_root / name
        source = plugin_path / relative_source
        try:
            ensure_path_within(normalized, plugin_path)
            ensure_path_within(source, plugin_path)
        except PathTraversalError:
            continue
        if (
            normalized.is_dir()
            and not normalized.is_symlink()
            and (normalized / "SKILL.md").is_file()
            and not (normalized / "SKILL.md").is_symlink()
            and source.is_dir()
            and not source.is_symlink()
            and (source / "SKILL.md").is_file()
            and not (source / "SKILL.md").is_symlink()
        ):
            resolved[name] = source
    return resolved, declared


def _write_plugin_skill_sources(
    plugin_path: Path,
    apm_dir: Path,
    sources: dict[str, Path],
    *,
    declared: bool,
) -> None:
    """Persist parser-owned membership and original byte sources for a plugin."""
    serialized = {
        name: source.relative_to(plugin_path).as_posix() for name, source in sorted(sources.items())
    }
    if apm_dir.is_symlink():
        raise PluginIntegrityError(
            f"Refusing to write plugin receipt through symlinked directory: {apm_dir}"
        )
    apm_dir.mkdir(parents=True, exist_ok=True)
    receipt = apm_dir / _PLUGIN_SKILL_SOURCES_FILE
    if receipt.is_symlink():
        raise PluginIntegrityError(f"Refusing to write symlinked plugin receipt: {receipt}")
    receipt.write_text(
        json.dumps({"version": 1, "declared": declared, "sources": serialized}, sort_keys=True),
        encoding="utf-8",
    )


def _remove_stale_normalized_skills(
    plugin_path: Path,
    target_skills: Path,
    current_sources: dict[str, Path],
) -> None:
    """Remove only prior parser-generated skill entries no longer declared."""
    previous_sources, _ = _read_plugin_skill_sources(plugin_path)
    for name in previous_sources.keys() - current_sources.keys():
        stale = target_skills / name
        if stale.exists():
            ensure_path_within(stale, plugin_path)
            if stale.is_symlink():
                raise PluginIntegrityError(
                    f"Refusing to remove symlinked plugin skill destination: {stale}"
                )
            shutil.rmtree(stale)


def _warn_skills_entry_holds_no_skill(entry: Path, plugin_path: Path) -> None:
    """Warn that a declared ``skills`` entry can never yield a skill.

    An entry with no ``SKILL.md`` at its root and none in any immediate child
    is neither a skill nor a container of them: it is copied under its own
    name, but nothing inside reaches ``.apm/skills/<name>/SKILL.md``, so it
    stays invisible to deployment and to ``--skill``. Silence here is what
    made #2530 expensive to diagnose -- the manifest looked wrong when only
    the directory depth was -- so say it once, at normalization time.
    """
    try:
        declared = entry.relative_to(plugin_path).as_posix()
    except ValueError:  # pragma: no cover - entries are verified inside the plugin
        declared = entry.name
    _surface_warning(
        f"Plugin skills entry '{declared}' has no SKILL.md at its root or in "
        f"any immediate subdirectory; nothing under it will deploy or be "
        f"selectable with --skill. Declare each skill directory, or place "
        f"skills one level below the declared container.",
        _logger,
    )


def parse_plugin_manifest(plugin_json_path: Path) -> dict[str, Any]:
    """Parse a plugin.json manifest file.

    Args:
        plugin_json_path: Path to the plugin.json file

    Returns:
        dict: Parsed plugin manifest

    Raises:
        FileNotFoundError: If plugin.json does not exist
        ValueError: If plugin.json is invalid JSON
    """
    if not plugin_json_path.exists():
        raise FileNotFoundError(f"plugin.json not found: {plugin_json_path}")

    try:
        manifest = _bounded_read_json(plugin_json_path)
    except ValueError as e:
        raise ValueError(f"Invalid JSON in plugin.json: {e}")  # noqa: B904

    if not manifest.get("name"):
        logging.getLogger("apm").warning(
            "plugin.json at %s is missing 'name' field; falling back to directory name",
            plugin_json_path,
        )

    return manifest


def normalize_plugin_directory(plugin_path: Path, plugin_json_path: Path | None = None) -> Path:
    """Normalize a Claude plugin directory into an APM package.

    Works with or without plugin.json.  When plugin.json is present it is
    treated as optional metadata; when absent the plugin name is derived from
    the directory name.

    Auto-discovers the standard component directories defined by the spec:
    agents/, commands/, skills/, hooks/, and pass-through files
    (.mcp.json, .lsp.json, settings.json).

    Args:
        plugin_path: Root of the plugin directory.
        plugin_json_path: Optional path to plugin.json (may be None).

    Returns:
        Path: Path to the generated apm.yml.
    """
    from ..agent_plugins.loader import admit_legacy_plugin_manifest

    admitted_manifest = admit_legacy_plugin_manifest(plugin_path)
    manifest: dict[str, Any] = admitted_manifest or {}
    root_manifest = plugin_path / "plugin.json"
    if (
        admitted_manifest is None
        and plugin_json_path is not None
        and plugin_json_path != root_manifest
        and plugin_json_path.exists()
    ):
        manifest = parse_plugin_manifest(plugin_json_path)
        from ..agent_plugins.errors import AgentPluginLegacyBoundaryError
        from ..bundle.local_bundle import PluginSchemaRoute, classify_plugin_manifest_schema

        if classify_plugin_manifest_schema(manifest) is PluginSchemaRoute.AGENT_PLUGIN:
            raise AgentPluginLegacyBoundaryError(
                "Schema-bearing plugin.json must be admitted from the package root, "
                "not Claude plugin normalization"
            )

    # Derive name from directory if not in manifest
    if "name" not in manifest or not manifest["name"]:
        if admitted_manifest is not None:
            raise ValueError("Present root plugin.json must declare a non-empty name")
        manifest["name"] = plugin_path.name

    return synthesize_apm_yml_from_plugin(plugin_path, manifest)


def _validate_declared_component_paths(plugin_path: Path, manifest: dict[str, Any]) -> None:
    """Fail when a plugin manifest declares a component that cannot be resolved."""
    plugin_name = str(manifest.get("name") or plugin_path.name)
    for field in ("agents", "skills", "commands", "hooks"):
        declared = manifest.get(field)
        if declared is None or declared == [] or (field == "hooks" and isinstance(declared, dict)):
            continue
        values = declared if isinstance(declared, list) else [declared]
        for value in values:
            declared_path = str(value)
            if not declared_path.strip():
                raise DeclaredPluginComponentError(
                    f"Plugin '{plugin_name}' declares an empty '{field}' component path "
                    f"in plugin root '{plugin_path}'. Remove the empty declaration "
                    "from plugin.json, then reinstall."
                )
            candidate = plugin_path / declared_path
            try:
                ensure_path_within(candidate, plugin_path)
                resolved = candidate.resolve()
            except (OSError, PathTraversalError, ValueError) as exc:
                raise DeclaredPluginComponentError(
                    f"Plugin '{plugin_name}' declares an invalid '{field}' component path "
                    f"'{declared_path}' outside plugin root '{plugin_path}'. "
                    "Move the component inside the plugin root or remove the declaration "
                    "from plugin.json, then reinstall."
                ) from exc
            if resolved.exists() and not candidate.is_symlink():
                continue
            raise DeclaredPluginComponentError(
                f"Plugin '{plugin_name}' declares missing '{field}' component path "
                f"'{declared_path}' in plugin root '{plugin_path}'. "
                "Add the component or remove the declaration from plugin.json, then reinstall."
            )


def synthesize_apm_yml_from_plugin(
    plugin_path: Path,
    manifest: dict[str, Any],
    *,
    output_path: Path | None = None,
    map_artifacts: bool = True,
    merge_existing: bool = True,
    substitute_plugin_root: bool = True,
    warn_on_invalid_servers: bool = True,
) -> Path:
    """Synthesize apm.yml from plugin metadata.

    Maps the plugin's agents/, skills/, commands/, hooks/ directories and
    pass-through files (.mcp.json, .lsp.json, settings.json) into .apm/,
    then generates apm.yml.

    When an existing ``apm.yml`` is present (dual-format packages that ship
    both ``plugin.json`` and ``apm.yml``), resolution-critical blocks --
    ``dependencies``, ``devDependencies``, ``registries``, ``targets``,
    ``includes``, ``scripts`` -- are preserved and merged with any plugin-
    derived dependencies so transitive resolution is not broken (#1666).

    Args:
        plugin_path: Path to the plugin directory.
        manifest: Plugin metadata dict (only `name` is required; all other
                  fields are optional and default gracefully).
        output_path: Optional staged manifest destination.
        map_artifacts: Whether to copy plugin artifacts into ``.apm``.
        merge_existing: Whether to preserve an existing package manifest.
        substitute_plugin_root: Whether to resolve plugin-root placeholders.
        warn_on_invalid_servers: Whether skipped server entries emit warnings.

    Returns:
        Path: Path to the generated apm.yml.
    """
    if not manifest.get("name"):
        manifest["name"] = plugin_path.name

    _validate_declared_component_paths(plugin_path, manifest)

    if map_artifacts:
        apm_dir = plugin_path / ".apm"
        apm_dir.mkdir(exist_ok=True)
        _map_plugin_artifacts(plugin_path, apm_dir, manifest)

    # Extract MCP servers from plugin and convert to dependency format
    mcp_servers = _extract_mcp_servers(
        plugin_path,
        manifest,
        substitute_plugin_root=substitute_plugin_root,
    )
    if mcp_servers:
        mcp_deps = _mcp_servers_to_apm_deps(
            mcp_servers,
            plugin_path,
            warn_on_invalid=warn_on_invalid_servers,
        )
        if mcp_deps:
            manifest["_mcp_deps"] = mcp_deps

    # Extract LSP servers from plugin and convert to dependency format
    lsp_servers = _extract_lsp_servers(
        plugin_path,
        manifest,
        substitute_plugin_root=substitute_plugin_root,
    )
    if lsp_servers:
        lsp_deps = lsp_servers_to_apm_deps(
            lsp_servers,
            plugin_path,
            warn_on_invalid=warn_on_invalid_servers,
        )
        if lsp_deps:
            manifest["_lsp_deps"] = lsp_deps

    # Load existing apm.yml as base so resolution-critical blocks are not
    # discarded when the synthesized manifest overwrites the file (#1666).
    source_apm_yml_path = plugin_path / "apm.yml"
    apm_yml_path = output_path or source_apm_yml_path
    existing_manifest: dict[str, Any] | None = None
    if merge_existing and source_apm_yml_path.exists():
        try:
            from ..utils.yaml_io import load_yaml

            data = load_yaml(source_apm_yml_path)
            if isinstance(data, dict):
                existing_manifest = data
        except (OSError, yaml.YAMLError) as exc:
            # Best-effort: fall back to plugin-only metadata. Surface a
            # warning so a malformed apm.yml does not silently re-introduce
            # the #1666 symptom (transitive deps dropped with no diagnostic).
            _surface_warning(
                f"Could not load existing apm.yml for merge; transitive "
                f"dependencies may not be preserved: {exc}",
                _logger,
            )

    # Generate apm.yml from plugin metadata, merging with existing manifest
    apm_yml_content = _generate_apm_yml(manifest, existing_manifest=existing_manifest)

    # LF-deterministic write (apm#2619): this synthetic manifest lands inside
    # the installed package tree that compute_package_hash() hashes raw, so a
    # platform-native text-mode write (CRLF on Windows) would make the
    # lockfile content_hash diverge across OSes. Mirrors the #2223 fix for
    # download_virtual_file_package().
    if output_path is None:
        write_text_lf(apm_yml_path, apm_yml_content)
    else:
        atomic_write_text(apm_yml_path, apm_yml_content, new_file_mode=0o644)

    return apm_yml_path


def _extract_mcp_servers(
    plugin_path: Path,
    manifest: dict[str, Any],
    *,
    substitute_plugin_root: bool = True,
) -> dict[str, Any]:
    """Extract MCP server definitions from a plugin manifest.

    Resolves ``mcpServers`` by type (per Claude Code spec):
    - ``str``  -> read that file path relative to plugin root, parse JSON,
      extract ``mcpServers`` key.
    - ``list`` -> read each file path, merge (last-wins on name conflict).
    - ``dict`` -> use directly as inline server definitions.

    When ``mcpServers`` is absent and ``mcp.json`` (or ``.mcp.json`` /
    ``.github/.mcp.json``)
    exists at plugin root, read it as the default (matches Claude Code
    auto-discovery).

    Security: symlinks are skipped, JSON parse errors are logged as warnings.

    ``${CLAUDE_PLUGIN_ROOT}`` in string values is replaced with the absolute
    plugin path.

    Args:
        plugin_path: Root of the plugin directory.
        manifest: Parsed plugin.json dict.

    Returns:
        dict mapping server name -> server config.  Empty on failure.
    """
    logger = logging.getLogger("apm")
    mcp_value = manifest.get("mcpServers")

    if mcp_value is not None:
        # Manifest explicitly defines mcpServers
        if isinstance(mcp_value, dict):
            servers = dict(mcp_value)
        elif isinstance(mcp_value, str):
            servers = _read_mcp_file(plugin_path, mcp_value, logger)
        elif isinstance(mcp_value, list):
            servers = {}
            for entry in mcp_value:
                if isinstance(entry, str):
                    servers.update(_read_mcp_file(plugin_path, entry, logger))
                else:
                    logger.warning("Ignoring non-string entry in mcpServers array: %s", entry)
        else:
            logger.warning("Unsupported mcpServers type %s; ignoring", type(mcp_value).__name__)
            return {}
    else:
        # Fall back to auto-discovery: mcp.json then .mcp.json then .github/.mcp.json
        servers = {}
        for fallback in ("mcp.json", ".mcp.json", ".github/.mcp.json"):
            candidate = plugin_path / fallback
            if candidate.exists() and candidate.is_file() and not candidate.is_symlink():
                servers = _read_mcp_json(candidate, logger)
                if servers:
                    break

    # Substitute ${CLAUDE_PLUGIN_ROOT} in all string values
    if servers and substitute_plugin_root:
        abs_root = str(plugin_path.resolve())
        servers = _substitute_plugin_root(servers, abs_root, logger)

    return servers


def _read_mcp_file(plugin_path: Path, rel_path: str, logger: logging.Logger) -> dict[str, Any]:
    """Read a JSON file relative to *plugin_path* and return its ``mcpServers`` dict."""
    target = (plugin_path / rel_path).resolve()
    # Security: must stay inside plugin_path and not be a symlink
    try:
        target.relative_to(plugin_path.resolve())
    except ValueError:
        logger.warning("MCP file path escapes plugin root: %s", rel_path)
        return {}
    candidate = plugin_path / rel_path
    if not candidate.exists() or not candidate.is_file():
        logger.warning("MCP file not found: %s", candidate)
        return {}
    if candidate.is_symlink():
        logger.warning("Skipping symlinked MCP file: %s", candidate)
        return {}
    return _read_mcp_json(candidate, logger)


def _read_mcp_json(path: Path, logger: logging.Logger) -> dict[str, Any]:
    """Parse a JSON file and return the ``mcpServers`` mapping."""
    try:
        data = _bounded_read_json(path)
    except (ValueError, OSError) as exc:
        logger.warning("Failed to read MCP config %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    servers = data.get("mcpServers", {})
    return dict(servers) if isinstance(servers, dict) else {}


def _substitute_plugin_root(
    servers: dict[str, Any], abs_root: str, logger: logging.Logger
) -> dict[str, Any]:
    """Replace ``${CLAUDE_PLUGIN_ROOT}`` in server config string values."""
    result = resolve_plugin_root_placeholders(servers, Path(abs_root))
    if result != servers:
        logger.info("Substituted ${CLAUDE_PLUGIN_ROOT} with %s", abs_root)
    return result


def resolve_plugin_root_placeholders(value: Any, plugin_path: Path) -> Any:
    """Resolve plugin-root placeholders in an in-memory manifest value."""
    if isinstance(value, str):
        return value.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_path.resolve()))
    if isinstance(value, dict):
        return {
            key: resolve_plugin_root_placeholders(item, plugin_path) for key, item in value.items()
        }
    if isinstance(value, list):
        return [resolve_plugin_root_placeholders(item, plugin_path) for item in value]
    return value


def _mcp_servers_to_apm_deps(
    servers: dict[str, Any],
    plugin_path: Path,
    *,
    warn_on_invalid: bool = True,
) -> list[dict[str, Any]]:
    """Convert raw MCP server configs to ``dependencies.mcp`` dicts.

    Transport inference:
    - ``command`` present -> stdio
    - ``url`` present -> http (or ``type`` if it's a valid transport)
    - Neither -> skipped with warning

    Every entry gets ``registry: false`` (self-defined, not registry lookups).

    All resulting entries are routed through ``MCPDependency.from_dict()``
    so plugin-synthesized servers must clear the same security validation
    chokepoint as CLI-authored or manually edited entries (name shape, URL
    scheme allowlist, header CRLF, command path-traversal). Entries that
    fail validation are skipped with a warning rather than crashing the
    plugin install -- a single malformed server should not block the
    whole plugin.

    Args:
        servers: Mapping of server name -> server config dict.
        plugin_path: Plugin root (used for log context only).

    Returns:
        List of dicts consumable by ``MCPDependency.from_dict()``.
    """
    from ..models.dependency.mcp import MCPDependency

    logger = logging.getLogger("apm")
    deps: list[dict[str, Any]] = []

    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            if warn_on_invalid:
                logger.warning("Skipping non-dict MCP server config '%s'", name)
            continue

        dep: dict[str, Any] = {"name": name, "registry": False}

        if "command" in cfg:
            dep["transport"] = "stdio"
            dep["command"] = cfg["command"]
            if "args" in cfg:
                dep["args"] = cfg["args"]
        elif "url" in cfg:
            raw_type = cfg.get("type", "http")
            valid_transports = {"http", "sse", "streamable-http"}
            dep["transport"] = raw_type if raw_type in valid_transports else "http"
            dep["url"] = cfg["url"]
            if "headers" in cfg:
                dep["headers"] = cfg["headers"]
        else:
            if warn_on_invalid:
                _surface_warning(
                    f"Skipping MCP server '{name}' from plugin "
                    f"'{plugin_path.name}': no 'command' or 'url'",
                    logger,
                )
            continue

        if "env" in cfg:
            dep["env"] = cfg["env"]
        if "tools" in cfg:
            dep["tools"] = cfg["tools"]

        # Route through the validation chokepoint. Plugins are an ingress
        # path: a malicious plugin could otherwise smuggle path traversal,
        # CRLF, or unsafe URL schemes that bypass MCPDependency.validate().
        # PR #809 follow-up: surface validation errors to the user via the
        # rich console (stdlib logger has no handlers configured).
        try:
            MCPDependency.from_dict(dep)
        except (ValueError, Exception) as exc:
            if warn_on_invalid:
                _surface_warning(
                    f"Skipping invalid MCP server '{name}' from plugin '{plugin_path.name}': {exc}",
                    logger,
                )
            continue

        deps.append(dep)

    return deps


def _extract_lsp_servers(
    plugin_path: Path,
    manifest: dict[str, Any],
    *,
    substitute_plugin_root: bool = True,
) -> dict[str, Any]:
    """Extract LSP server definitions from a plugin manifest.

    Resolves ``lspServers`` by type (per Claude Code spec):
    - ``str``  -> read that file path relative to plugin root, parse JSON.
    - ``dict`` -> use directly as inline server definitions.

    When ``lspServers`` is absent and ``lsp.json`` / ``.lsp.json`` exists at
    plugin root, read it as the default (matches Claude Code auto-discovery).

    Security: symlinks are skipped, JSON parse errors are logged as warnings.

    ``${CLAUDE_PLUGIN_ROOT}`` in string values is replaced with the absolute
    plugin path.

    Args:
        plugin_path: Root of the plugin directory.
        manifest: Parsed plugin.json dict.

    Returns:
        dict mapping server name -> server config.  Empty on failure.
    """
    logger = logging.getLogger("apm")
    lsp_value = manifest.get("lspServers")

    if lsp_value is not None:
        if isinstance(lsp_value, dict):
            servers = dict(lsp_value)
        elif isinstance(lsp_value, str):
            servers = _read_lsp_file(plugin_path, lsp_value, logger)
        else:
            logger.warning("Unsupported lspServers type %s; ignoring", type(lsp_value).__name__)
            return {}
    else:
        # Fall back to auto-discovery: com.microsoft.apm/lsp.json, lsp.json, then .lsp.json
        servers = {}
        for fallback in ("com.microsoft.apm/lsp.json", "lsp.json", ".lsp.json"):
            candidate = plugin_path / fallback
            if candidate.exists() and candidate.is_file() and not candidate.is_symlink():
                servers = _read_lsp_json(candidate, logger)
                if servers:
                    break

    # Substitute ${CLAUDE_PLUGIN_ROOT} in all string values
    if servers and substitute_plugin_root:
        abs_root = str(plugin_path.resolve())
        servers = _substitute_plugin_root(servers, abs_root, logger)

    return servers


def _read_lsp_file(plugin_path: Path, rel_path: str, logger: logging.Logger) -> dict[str, Any]:
    """Read a JSON file relative to *plugin_path* and return its LSP server dict."""
    target = (plugin_path / rel_path).resolve()
    try:
        target.relative_to(plugin_path.resolve())
    except ValueError:
        logger.warning("LSP file path escapes plugin root: %s", rel_path)
        return {}
    candidate = plugin_path / rel_path
    if not candidate.exists() or not candidate.is_file():
        logger.warning("LSP file not found: %s", candidate)
        return {}
    if candidate.is_symlink():
        logger.warning("Skipping symlinked LSP file: %s", candidate)
        return {}
    return _read_lsp_json(candidate, logger)


def _read_lsp_json(path: Path, logger: logging.Logger) -> dict[str, Any]:
    """Parse a JSON file and return the LSP servers mapping.

    Accepts two formats:
    - Flat: top-level keys are server names (e.g. ``{"pyright": {...}}``).
    - Wrapped: a ``"lspServers"`` envelope wraps the servers
      (e.g. ``{"lspServers": {"pyright": {...}}}``).

    The wrapped format is standard in Copilot ``.github/lsp.json`` and
    Claude ``~/.claude.json``.  Plugins may ship either variant.
    """
    try:
        data = _bounded_read_json(path)
    except (ValueError, OSError) as exc:
        logger.warning("Failed to read LSP config %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    # Unwrap the { "lspServers": { ... } } envelope when present.
    # Only unwrap when the inner value looks like a server *map* (all values
    # are dicts).  A flat-format server literally named "lspServers" would
    # have scalar values like "command", so the all-dicts check avoids
    # mis-detecting it as an envelope.
    lsp_inner = data.get("lspServers")
    if (
        isinstance(lsp_inner, dict)
        and lsp_inner
        and all(isinstance(v, dict) for v in lsp_inner.values())
    ):
        logger.debug("Unwrapped lspServers envelope in %s", path)
        return dict(lsp_inner)
    return dict(data)


def lsp_servers_to_apm_deps(
    servers: dict[str, Any],
    plugin_path: Path,
    *,
    warn_on_invalid: bool = True,
) -> list[dict[str, Any]]:
    """Convert raw LSP server configs to ``dependencies.lsp`` dicts.

    Required fields per Claude Code spec:
    - ``command``: binary to run
    - ``extensionToLanguage``: mapping of file extensions to language IDs

    Copilot-dialect spellings are accepted as aliases (#2509): plugins
    authored against the Copilot CLI schema -- including the official
    ``dotnet/skills`` dotnet plugin -- write the extension map as
    ``fileExtensions`` and the startup budget as ``warmupTimeoutMs``.
    APM itself emits ``fileExtensions`` when generating Copilot output,
    so rejecting it on intake would drop servers that every supported
    consumer runtime accepts. Canonical names win when both are present.

    All resulting entries are routed through ``LSPDependency.from_dict()``
    for validation. Entries that fail validation are skipped with a warning.

    Args:
        servers: Mapping of server name -> server config dict.
        plugin_path: Plugin root (used for log context only).

    Returns:
        List of dicts consumable by ``LSPDependency.from_dict()``.
    """
    from ..models.dependency.lsp import LSPDependency

    logger = logging.getLogger("apm")
    deps: list[dict[str, Any]] = []

    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            if warn_on_invalid:
                logger.warning("Skipping non-dict LSP server config '%s'", name)
            continue

        dep: dict[str, Any] = {"name": name}
        aliases_used: list[tuple[str, str]] = []

        # Copy all recognized fields
        for key in (
            "command",
            "args",
            "extensionToLanguage",
            "transport",
            "env",
            "initializationOptions",
            "settings",
            "workspaceFolder",
            "startupTimeout",
            "shutdownTimeout",
            "restartOnCrash",
            "maxRestarts",
        ):
            if key in cfg:
                dep[key] = cfg[key]

        # Copilot-dialect aliases; canonical spelling wins when both exist.
        for canonical, alias in _LSP_FIELD_ALIASES:
            if dep.get(canonical) is None and alias in cfg:
                dep[canonical] = cfg[alias]
                aliases_used.append((alias, canonical))
                logger.debug(
                    "Normalizing LSP server '%s' from plugin '%s': '%s' to '%s'",
                    name,
                    plugin_path.name,
                    alias,
                    canonical,
                )
            elif canonical in dep and alias in cfg and dep[canonical] != cfg[alias]:
                _surface_warning(
                    f"LSP server '{name}' from plugin '{plugin_path.name}' defines both "
                    f"'{canonical}' and '{alias}'; using '{canonical}'.",
                    logger,
                )

        # ``cwd`` has no LSPDependency equivalent: APM-managed servers are
        # started by the consumer runtime in its own working directory.
        # Ignore it explicitly rather than letting it look like a typo.
        if "cwd" in cfg:
            _surface_warning(
                f"LSP server '{name}' from plugin '{plugin_path.name}' uses unsupported "
                "'cwd'; the consumer runtime chooses the working directory.",
                logger,
            )

        # Route through the validation chokepoint
        try:
            LSPDependency.from_dict(dep)
        except Exception as exc:
            if warn_on_invalid:
                alias_context = ""
                if aliases_used:
                    normalized = ", ".join(
                        f"'{alias}' to '{canonical}'" for alias, canonical in aliases_used
                    )
                    alias_context = f" after normalizing {normalized}"
                _surface_warning(
                    f"Skipping invalid LSP server '{name}' from plugin "
                    f"'{plugin_path.name}'{alias_context}: {exc}",
                    logger,
                )
            continue

        deps.append(dep)

    return deps


_lsp_servers_to_apm_deps = lsp_servers_to_apm_deps


def _map_plugin_artifacts(
    plugin_path: Path, apm_dir: Path, manifest: dict[str, Any] | None = None
) -> None:
    """Map plugin artifacts to .apm/ subdirectories and copy pass-through files.

    Copies:
    - agents/     -> .apm/agents/
    - skills/     -> .apm/skills/
    - commands/   -> .apm/prompts/  (*.md normalized to *.prompt.md)
    - hooks/      -> .apm/hooks/    (directory, config file, or inline object)
    - .mcp.json   -> .apm/.mcp.json  (MCP-based plugins need this to function)
    - .lsp.json   -> .apm/.lsp.json
    - settings.json -> .apm/settings.json

    When the manifest specifies custom component paths (e.g. ``"agents": ["custom/"]``),
    those paths are used instead of the defaults.

    Symlinks are skipped entirely to prevent content exfiltration attacks.

    Args:
        plugin_path: Root of the plugin directory.
        apm_dir: Path to the .apm/ directory.
        manifest: Optional plugin.json metadata; used for custom component paths.
    """
    if manifest is None:
        manifest = {}

    from apm_cli.security.gate import ignore_non_content as _ignore_non_content

    if apm_dir.is_symlink():
        raise PluginIntegrityError(
            f"Refusing to map plugin artifacts through symlinked destination: {apm_dir}"
        )
    plugin_path = plugin_path.resolve()
    # Command copying checks every discovered file, so resolve the staging
    # root once rather than repeating filesystem resolution per path.
    staging_root = apm_dir.resolve()
    staging_parent = apm_dir.parent.resolve()
    staging_name = apm_dir.name

    def ignore_non_content(directory: str, contents: list[str]) -> list[str]:
        """Compose the canonical content filter with staging-root containment."""
        ignored = set(_ignore_non_content(directory, contents))
        try:
            copying_from_plugin_root = Path(directory).resolve() == staging_parent
        except OSError:
            copying_from_plugin_root = True
        if copying_from_plugin_root and staging_name in contents:
            ignored.add(staging_name)
        return list(ignored)

    # Resolve source paths  -- use manifest arrays if present, else defaults.
    # Custom paths may be directories OR individual files.
    #
    # Security: every manifest-controlled path is verified to resolve
    # inside *plugin_path* before it is copied.  Without this guard, a
    # malicious plugin could set ``"commands": "/etc/passwd"`` or
    # ``"agents": ["../../host"]`` and trick ``apm install`` into copying
    # arbitrary host files into the project's ``.apm/`` tree (and from
    # there into ``.github/prompts/`` via auto-integration).
    def _resolve_sources(component: str, default_dir: str):
        """Return list of existing source paths (dirs or files) for a component."""
        custom = manifest.get(component)
        if component == "skills" and component in manifest and not isinstance(custom, (list, str)):
            return []
        if isinstance(custom, list):
            paths = []
            for p in custom:
                if not isinstance(p, str):
                    continue
                raw = p
                src = plugin_path / raw
                if (
                    src.exists()
                    and not _has_symlink_component(src, plugin_path)
                    and _is_within_plugin(src, plugin_path, component=component)
                ):
                    paths.append(src.resolve())
            return paths
        elif isinstance(custom, str):
            src = plugin_path / custom
            if (
                src.exists()
                and not _has_symlink_component(src, plugin_path)
                and _is_within_plugin(src, plugin_path, component=component)
            ):
                return [src.resolve()]
            return []
        default = plugin_path / default_dir
        if (
            default.exists()
            and not _has_symlink_component(default, plugin_path)
            and default.is_dir()
            and _is_within_plugin(default, plugin_path, component=component)
        ):
            return [default.resolve()]
        return []

    # Helper: True when *src* and *dst* resolve to the same filesystem path
    # (e.g. a manifest entry pointing at a file already inside the target).
    # Copying onto self raises ``shutil.SameFileError`` and ``shutil.copytree``
    # over identical directories triggers it per-file, so callers must skip.
    def _is_same_path(src: Path, dst: Path) -> bool:
        try:
            return src.resolve() == dst.resolve()
        except OSError:
            return False

    # Map agents/
    # Unlike skills (which are named directories containing SKILL.md), agents
    # are flat files  -- each .md is one agent.  So we always merge directory
    # contents directly into .apm/agents/ (no nesting by dir name).
    agent_sources = _resolve_sources("agents", "agents")
    if agent_sources:
        target_agents = apm_dir / "agents"
        _assert_no_symlink_descendants(target_agents)
        agent_dirs = [s for s in agent_sources if s.is_dir()]
        agent_files = [s for s in agent_sources if s.is_file()]
        for d in agent_dirs:
            if _is_same_path(d, target_agents):
                continue
            shutil.copytree(
                d,
                target_agents,
                dirs_exist_ok=True,
                ignore=ignore_non_content,
            )
        if agent_files:
            target_agents.mkdir(parents=True, exist_ok=True)
            for f in agent_files:
                dst = target_agents / f.name
                if _is_same_path(f, dst):
                    continue
                shutil.copy2(f, dst)

    _map_plugin_skills(
        plugin_path,
        apm_dir,
        manifest,
        _resolve_sources,
        _is_same_path,
        ignore_non_content,
    )

    # Map commands/ -> .apm/prompts/ (normalize .md -> .prompt.md)
    command_sources = _resolve_sources("commands", "commands")
    if command_sources:
        target_prompts = apm_dir / "prompts"
        _assert_no_symlink_descendants(target_prompts)
        target_prompts.mkdir(parents=True, exist_ok=True)
        target_prompts_root = target_prompts.resolve()

        def _copy_command_file(source_file: Path, dest_dir: Path, rel_to: Path = None):  # noqa: RUF013
            """Copy a command file, normalizing .md -> .prompt.md."""
            if rel_to:
                relative_path = source_file.relative_to(rel_to)
                target_path = dest_dir / relative_path
            else:
                target_path = dest_dir / source_file.name
            if not source_file.name.endswith(".prompt.md") and source_file.suffix == ".md":
                target_path = target_path.with_name(f"{source_file.stem}.prompt.md")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if _is_same_path(source_file, target_path):
                return
            shutil.copy2(source_file, target_path)

        for source in command_sources:
            if source.is_file() and not source.is_symlink():
                _copy_command_file(source, target_prompts)
            elif source.is_dir():
                for source_file, source_root in _iter_command_source_files(
                    source,
                    target_prompts_root,
                    staging_root,
                    staging_parent,
                    _ignore_non_content,
                ):
                    _copy_command_file(source_file, target_prompts, rel_to=source_root)

    # Map hooks/  -- the spec allows a directory path, a config file path,
    # or an inline object.  Handle all three forms.
    hooks_value = manifest.get("hooks")
    if isinstance(hooks_value, dict):
        # Inline hooks object -> write as .apm/hooks/hooks.json.
        # LF-deterministic + UTF-8 write (apm#2619): this file lands inside
        # the hashed package tree, so a platform-native write (CRLF on
        # Windows, locale codec) would make the lockfile content_hash
        # diverge across OSes.
        target_hooks = apm_dir / "hooks"
        _assert_no_symlink_descendants(target_hooks)
        target_hooks.mkdir(parents=True, exist_ok=True)
        write_text_lf(target_hooks / "hooks.json", json.dumps(hooks_value, indent=2))
    elif isinstance(hooks_value, str) and (plugin_path / hooks_value).is_file():
        # Config file path (e.g. "hooks": "hooks.json")
        src_file = plugin_path / hooks_value
        if src_file.is_symlink() or not _is_within_plugin(src_file, plugin_path, component="hooks"):
            pass
        else:
            target_hooks = apm_dir / "hooks"
            _assert_no_symlink_descendants(target_hooks)
            target_hooks.mkdir(parents=True, exist_ok=True)
            dst = target_hooks / "hooks.json"
            if not _is_same_path(src_file, dst):
                shutil.copy2(src_file, dst)
    else:
        # Directory path(s)  -- standard flow
        hook_sources = _resolve_sources("hooks", "hooks")
        if hook_sources:
            target_hooks = apm_dir / "hooks"
            _assert_no_symlink_descendants(target_hooks)
            for d in hook_sources:
                if _is_same_path(d, target_hooks):
                    continue
                shutil.copytree(
                    d,
                    target_hooks,
                    dirs_exist_ok=True,
                    ignore=ignore_non_content,
                )

    # Pass-through files required for MCP/LSP plugins to function
    for passthrough in (".mcp.json", ".lsp.json", "settings.json"):
        source_file = plugin_path / passthrough
        if source_file.exists() and not source_file.is_symlink():
            dst = apm_dir / passthrough
            if dst.is_symlink():
                raise PluginIntegrityError(
                    f"Refusing to copy through symlinked plugin pass-through file: {dst}"
                )
            if not _is_same_path(source_file, dst):
                shutil.copy2(source_file, dst)


def _generate_apm_yml(
    manifest: dict[str, Any],
    existing_manifest: dict[str, Any] | None = None,
) -> str:
    """Generate apm.yml content from plugin metadata.

    When *existing_manifest* is provided (from a pre-existing ``apm.yml``),
    resolution-critical blocks are preserved so transitive dependency
    resolution is not broken for dual-format packages (#1666).

    Args:
        manifest: Plugin metadata dict.
        existing_manifest: Pre-existing ``apm.yml`` data, or ``None``.

    Returns:
        str: YAML content for apm.yml.
    """
    apm_package: dict[str, Any] = {
        "name": manifest.get("name") or (existing_manifest or {}).get("name"),
        "version": manifest.get("version") or (existing_manifest or {}).get("version", "0.0.0"),
        "description": manifest.get("description")
        or (existing_manifest or {}).get("description", ""),
    }

    # author: spec defines it as {name, email, url} object; accept string too
    if "author" in manifest:
        author = manifest["author"]
        if isinstance(author, dict):
            apm_package["author"] = author.get("name", "")
        else:
            apm_package["author"] = str(author)
    elif existing_manifest and "author" in existing_manifest:
        apm_package["author"] = existing_manifest["author"]

    for field in ("license", "repository", "homepage", "tags"):
        value = manifest.get(field) or (existing_manifest or {}).get(field)
        if value is not None:
            apm_package[field] = value

    # --- Dependency merging (#1666) ---
    # Start from the existing manifest's dependencies so they are not
    # discarded, then layer in any plugin-derived dependencies.
    merged_deps: dict[str, Any] = {}
    if existing_manifest:
        existing_deps = existing_manifest.get("dependencies")
        if isinstance(existing_deps, dict):
            for key, val in existing_deps.items():
                if isinstance(val, list):
                    merged_deps[key] = list(val)

    plugin_deps = manifest.get("dependencies")
    if plugin_deps:
        if isinstance(plugin_deps, list):
            _union_dep_list(merged_deps, "apm", plugin_deps)
        else:
            # Plugin.json may declare deps as a dict (name -> version).
            # Preserve the original shape under dependencies.apm.
            merged_deps.setdefault("apm", plugin_deps)

    # Inject MCP deps extracted from plugin mcpServers / .mcp.json
    mcp_deps = manifest.get("_mcp_deps")
    if mcp_deps:
        _union_dep_list(merged_deps, "mcp", mcp_deps)

    # Inject LSP deps extracted from plugin lspServers / .lsp.json
    lsp_deps = manifest.get("_lsp_deps")
    if lsp_deps:
        _union_dep_list(merged_deps, "lsp", lsp_deps)

    if merged_deps:
        apm_package["dependencies"] = merged_deps

    # Preserve other resolution-critical blocks from the existing manifest
    # so registries, targets, scripts, devDependencies and includes are
    # not silently discarded (#1666).
    if existing_manifest:
        for key in (
            "devDependencies",
            "registries",
            "target",
            "targets",
            "includes",
            "scripts",
        ):
            if key in existing_manifest and key not in apm_package:
                apm_package[key] = existing_manifest[key]

    # Install behavior is driven by file presence (SKILL.md, etc.), not this
    # field.  Default to hybrid so the standard pipeline handles all components.
    apm_package["type"] = "hybrid"

    from ..utils.yaml_io import yaml_to_str

    return yaml_to_str(apm_package)


def _union_dep_list(
    merged: dict[str, list[Any]],
    key: str,
    new_entries: list[Any],
) -> None:
    """Append *new_entries* into ``merged[key]`` without duplicates.

    Both string entries and nested mapping entries are handled. A hashable
    structural key keeps merging linear while preserving equality semantics.
    """
    existing = merged.setdefault(key, [])
    seen = {_dependency_entry_key(entry) for entry in existing}
    for entry in new_entries:
        entry_key = _dependency_entry_key(entry)
        if entry_key in seen:
            continue
        existing.append(entry)
        seen.add(entry_key)


def _dependency_entry_key(value: Any) -> Any:
    """Return a hashable structural key for a manifest dependency entry."""
    if isinstance(value, dict):
        return (
            "dict",
            frozenset((key, _dependency_entry_key(item)) for key, item in value.items()),
        )
    if isinstance(value, list):
        return ("list", tuple(_dependency_entry_key(item) for item in value))
    if isinstance(value, tuple):
        return ("tuple", tuple(_dependency_entry_key(item) for item in value))
    try:
        hash(value)
    except TypeError:
        return ("repr", repr(value))
    return ("scalar", type(value).__qualname__, value)


def synthesize_plugin_json_from_apm_yml(apm_yml_path: Path) -> dict:
    """Create a minimal ``plugin.json`` dict from ``apm.yml`` identity fields.

    Reads ``apm.yml`` and extracts ``name``, ``version``, ``description``,
    ``author``, ``license``, ``homepage``, ``repository``, and ``keywords``.

    The ``author`` field accepts either a plain string or a structured object
    with ``name``, ``email``, and ``url`` keys.  A plain string is mapped to
    ``{"name": author}``; a dict passes through its recognized keys.

    Args:
        apm_yml_path: Path to the ``apm.yml`` file.

    Returns:
        dict suitable for writing as ``plugin.json``.

    Raises:
        ValueError: If ``name`` is missing from ``apm.yml``.
        FileNotFoundError: If the file does not exist.
    """
    if not apm_yml_path.exists():
        raise FileNotFoundError(f"apm.yml not found: {apm_yml_path}")

    try:
        from ..utils.yaml_io import load_yaml

        data = load_yaml(apm_yml_path)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {apm_yml_path}: {exc}") from exc

    if not isinstance(data, dict) or not data.get("name"):
        raise ValueError("apm.yml must contain at least a 'name' field to synthesize plugin.json")

    result: dict[str, Any] = {"name": data["name"]}

    if data.get("version"):
        result["version"] = data["version"]
    if data.get("description"):
        result["description"] = data["description"]
    if data.get("author"):
        author = data["author"]
        if isinstance(author, dict):
            # name is required for the structured path; drop the author field if absent
            if author.get("name"):
                author_obj: dict[str, str] = {"name": str(author["name"])}
                if author.get("email"):
                    author_obj["email"] = str(author["email"])
                if author.get("url"):
                    author_obj["url"] = str(author["url"])
                result["author"] = author_obj
        else:
            result["author"] = {"name": str(author)}
    if data.get("license"):
        result["license"] = data["license"]
    if data.get("homepage"):
        result["homepage"] = str(data["homepage"])
    if data.get("repository"):
        result["repository"] = str(data["repository"])
    if data.get("keywords"):
        raw_kw = data["keywords"]
        result["keywords"] = [str(raw_kw)] if isinstance(raw_kw, str) else [str(k) for k in raw_kw]

    return result


def validate_plugin_package(plugin_path: Path) -> bool:
    """Check whether a directory looks like a Claude plugin.

    A directory is a valid plugin if it has plugin.json (with at least a name),
    or if it contains at least one standard component directory.

    Args:
        plugin_path: Path to the plugin directory.

    Returns:
        bool: True if the directory appears to be a Claude plugin.
    """
    # Check for plugin.json (optional; only name is required when present)
    from ..utils.helpers import find_plugin_json

    plugin_json = find_plugin_json(plugin_path)
    if plugin_json is not None:
        try:
            manifest = _bounded_read_json(plugin_json)
        except (ValueError, OSError):
            pass
        else:
            if isinstance(manifest, dict) and manifest.get("name"):
                return True

    # Fallback: presence of any standard component directory
    for component_dir in ("agents", "commands", "skills", "hooks"):
        if (plugin_path / component_dir).is_dir():
            return True

    return False
