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
from pathlib import Path
from typing import Any

import yaml

from ..utils.console import _rich_warning
from ..utils.path_security import PathTraversalError, ensure_path_within

# Rule A re-export: implementations in plugin_server_helpers; names stay resolvable here.
from .plugin_server_helpers import (
    _extract_lsp_servers as _extract_lsp_servers,
)
from .plugin_server_helpers import (
    _extract_mcp_servers as _extract_mcp_servers,
)
from .plugin_server_helpers import (
    _lsp_servers_to_apm_deps as _lsp_servers_to_apm_deps,
)
from .plugin_server_helpers import (
    _mcp_servers_to_apm_deps as _mcp_servers_to_apm_deps,
)
from .plugin_server_helpers import (
    _read_lsp_file as _read_lsp_file,
)
from .plugin_server_helpers import (
    _read_lsp_json as _read_lsp_json,
)
from .plugin_server_helpers import (
    _read_mcp_file as _read_mcp_file,
)
from .plugin_server_helpers import (
    _read_mcp_json as _read_mcp_json,
)
from .plugin_server_helpers import (
    _substitute_plugin_root as _substitute_plugin_root,
)

_logger = logging.getLogger(__name__)


class PluginIntegrityError(RuntimeError):
    """Raised when a plugin destination tree contains a pre-existing symlink.

    Refusing to copy through a symlinked destination is defense-in-depth
    for the data-loss-adjacent ``shutil.copytree(..., dirs_exist_ok=True)``
    flow in ``_map_plugin_artifacts``. A malicious package shipping
    ``.apm/skills/<name>`` (or any other target_* subtree) as a symlink to
    an external path (e.g. ``/etc``, ``$HOME/.ssh``) would otherwise
    redirect writes outside the plugin root.
    """


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
    """Emit a warning to both the stdlib logger and the rich console.

    The ``apm`` stdlib logger has no handlers configured by default, so
    ``logger.warning`` calls are silently dropped in non-debug runs. For
    user-visible plugin-parse issues (skipped MCP servers, validation
    failures), also route through ``_rich_warning`` so the user sees them
    even without ``--verbose``. Falls back gracefully if Rich is unavailable.
    """
    logger.warning(message)
    try:  # noqa: SIM105
        _rich_warning(message, symbol="warning")
    except Exception:
        # Console output is best-effort; never mask the underlying warning.
        pass


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
        with open(plugin_json_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
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
    manifest: dict[str, Any] = {}

    if plugin_json_path is not None and plugin_json_path.exists():
        try:  # noqa: SIM105
            manifest = parse_plugin_manifest(plugin_json_path)
        except (ValueError, FileNotFoundError):
            pass  # Treat as empty manifest; fall back to dir-name defaults

    # Derive name from directory if not in manifest
    if "name" not in manifest or not manifest["name"]:
        manifest["name"] = plugin_path.name

    return synthesize_apm_yml_from_plugin(plugin_path, manifest)


def synthesize_apm_yml_from_plugin(plugin_path: Path, manifest: dict[str, Any]) -> Path:
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

    Returns:
        Path: Path to the generated apm.yml.
    """
    if not manifest.get("name"):
        manifest["name"] = plugin_path.name

    # Create .apm directory structure
    apm_dir = plugin_path / ".apm"
    apm_dir.mkdir(exist_ok=True)

    # Map plugin structure into .apm/ subdirectories
    _map_plugin_artifacts(plugin_path, apm_dir, manifest)

    # Extract MCP servers from plugin and convert to dependency format
    mcp_servers = _extract_mcp_servers(plugin_path, manifest)
    if mcp_servers:
        mcp_deps = _mcp_servers_to_apm_deps(mcp_servers, plugin_path)
        if mcp_deps:
            manifest["_mcp_deps"] = mcp_deps

    # Extract LSP servers from plugin and convert to dependency format
    lsp_servers = _extract_lsp_servers(plugin_path, manifest)
    if lsp_servers:
        lsp_deps = _lsp_servers_to_apm_deps(lsp_servers, plugin_path)
        if lsp_deps:
            manifest["_lsp_deps"] = lsp_deps

    # Load existing apm.yml as base so resolution-critical blocks are not
    # discarded when the synthesized manifest overwrites the file (#1666).
    apm_yml_path = plugin_path / "apm.yml"
    existing_manifest: dict[str, Any] | None = None
    if apm_yml_path.exists():
        try:
            from ..utils.yaml_io import load_yaml

            data = load_yaml(apm_yml_path)
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

    with open(apm_yml_path, "w", encoding="utf-8") as f:
        f.write(apm_yml_content)

    return apm_yml_path


# ---------------------------------------------------------------------------
# _map_plugin_artifacts sub-helpers (module-level so they can be tested
# independently and to keep _map_plugin_artifacts below C901=35).
# ---------------------------------------------------------------------------


def _resolve_plugin_sources(
    plugin_path: Path, manifest: dict[str, Any], component: str, default_dir: str
) -> list[Path]:
    """Return list of existing source paths (dirs or files) for *component*.

    Uses ``manifest[component]`` when present (list or str), else falls
    back to the ``default_dir`` directory inside *plugin_path*.  Every
    path is verified to exist, not be a symlink, and resolve inside
    *plugin_path* (path-traversal guard).
    """
    custom = manifest.get(component)
    if isinstance(custom, list):
        paths = []
        for p in custom:
            src = plugin_path / str(p)
            if (
                src.exists()
                and not src.is_symlink()
                and _is_within_plugin(src, plugin_path, component=component)
            ):
                paths.append(src)
        return paths
    if isinstance(custom, str):
        src = plugin_path / custom
        if (
            src.exists()
            and not src.is_symlink()
            and _is_within_plugin(src, plugin_path, component=component)
        ):
            return [src]
        return []
    default = plugin_path / default_dir
    if (
        default.exists()
        and not default.is_symlink()
        and default.is_dir()
        and _is_within_plugin(default, plugin_path, component=component)
    ):
        return [default]
    return []


def _is_same_path(src: Path, dst: Path) -> bool:
    """Return True when *src* and *dst* resolve to the same filesystem path.

    Copying onto self raises ``shutil.SameFileError``; callers must skip.
    """
    try:
        return src.resolve() == dst.resolve()
    except OSError:
        return False


def _copy_plugin_command_file(
    source_file: Path, dest_dir: Path, rel_to: Path | None = None
) -> None:
    """Copy a command file into *dest_dir*, normalising ``.md`` -> ``.prompt.md``."""
    if rel_to is not None:
        relative_path = source_file.relative_to(rel_to)
        target_path = dest_dir / relative_path
    else:
        target_path = dest_dir / source_file.name
    if not source_file.name.endswith(".prompt.md") and source_file.suffix == ".md":
        target_path = target_path.with_name(f"{source_file.stem}.prompt.md")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if _is_same_path(source_file, target_path):
        return
    import shutil

    shutil.copy2(source_file, target_path)


def _map_plugin_agents(agent_sources: list[Path], apm_dir: Path) -> None:
    """Copy agent sources into ``.apm/agents/``."""
    import shutil

    from apm_cli.security.gate import ignore_non_content

    target_agents = apm_dir / "agents"
    _assert_no_symlink_descendants(target_agents)
    agent_dirs = [s for s in agent_sources if s.is_dir()]
    agent_files = [s for s in agent_sources if s.is_file()]
    for d in agent_dirs:
        if _is_same_path(d, target_agents):
            continue
        shutil.copytree(d, target_agents, dirs_exist_ok=True, ignore=ignore_non_content)
    if agent_files:
        target_agents.mkdir(parents=True, exist_ok=True)
        for f in agent_files:
            dst = target_agents / f.name
            if not _is_same_path(f, dst):
                shutil.copy2(f, dst)


def _map_plugin_skills(skill_sources: list[Path], apm_dir: Path, manifest: dict[str, Any]) -> None:
    """Copy skill sources into ``.apm/skills/``."""
    import shutil

    from apm_cli.security.gate import ignore_non_content

    target_skills = apm_dir / "skills"
    _assert_no_symlink_descendants(target_skills)
    skill_dirs = [s for s in skill_sources if s.is_dir()]
    skill_files = [s for s in skill_sources if s.is_file()]
    is_custom_list = isinstance(manifest.get("skills"), list)
    if is_custom_list and skill_dirs:
        target_skills.mkdir(parents=True, exist_ok=True)
        for d in skill_dirs:
            nested = target_skills / d.name
            if not _is_same_path(d, nested):
                shutil.copytree(d, nested, ignore=ignore_non_content, dirs_exist_ok=True)
    elif skill_dirs:
        for d in skill_dirs:
            if not _is_same_path(d, target_skills):
                shutil.copytree(d, target_skills, dirs_exist_ok=True, ignore=ignore_non_content)
    if skill_files:
        target_skills.mkdir(parents=True, exist_ok=True)
        for f in skill_files:
            dst = target_skills / f.name
            if not _is_same_path(f, dst):
                shutil.copy2(f, dst)


def _map_plugin_commands(command_sources: list[Path], apm_dir: Path) -> None:
    """Copy command sources into ``.apm/prompts/``, normalising ``.md`` -> ``.prompt.md``."""
    target_prompts = apm_dir / "prompts"
    _assert_no_symlink_descendants(target_prompts)
    target_prompts.mkdir(parents=True, exist_ok=True)
    for source in command_sources:
        if source.is_file() and not source.is_symlink():
            _copy_plugin_command_file(source, target_prompts)
        elif source.is_dir():
            for source_file in source.rglob("*"):
                if not source_file.is_file() or source_file.is_symlink():
                    continue
                _copy_plugin_command_file(source_file, target_prompts, rel_to=source)


def _map_plugin_hooks(manifest: dict[str, Any], plugin_path: Path, apm_dir: Path) -> None:
    """Map hooks into ``.apm/hooks/``.

    The spec allows a directory path, a config file path, or an inline
    object.  All three forms are handled.
    """
    import json
    import shutil

    from apm_cli.security.gate import ignore_non_content

    hooks_value = manifest.get("hooks")
    if isinstance(hooks_value, dict):
        # Inline hooks object -> write as .apm/hooks/hooks.json
        target_hooks = apm_dir / "hooks"
        _assert_no_symlink_descendants(target_hooks)
        target_hooks.mkdir(parents=True, exist_ok=True)
        (target_hooks / "hooks.json").write_text(json.dumps(hooks_value, indent=2))
    elif isinstance(hooks_value, str) and (plugin_path / hooks_value).is_file():
        # Config file path (e.g. "hooks": "hooks.json")
        src_file = plugin_path / hooks_value
        if not src_file.is_symlink() and _is_within_plugin(
            src_file, plugin_path, component="hooks"
        ):
            target_hooks = apm_dir / "hooks"
            _assert_no_symlink_descendants(target_hooks)
            target_hooks.mkdir(parents=True, exist_ok=True)
            dst = target_hooks / "hooks.json"
            if not _is_same_path(src_file, dst):
                shutil.copy2(src_file, dst)
    else:
        # Directory path(s)  -- standard flow
        hook_sources = _resolve_plugin_sources(plugin_path, manifest, "hooks", "hooks")
        if hook_sources:
            target_hooks = apm_dir / "hooks"
            _assert_no_symlink_descendants(target_hooks)
            for d in hook_sources:
                if not _is_same_path(d, target_hooks):
                    shutil.copytree(d, target_hooks, dirs_exist_ok=True, ignore=ignore_non_content)


def _copy_plugin_passthrough_files(plugin_path: Path, apm_dir: Path) -> None:
    """Copy ``.mcp.json``, ``.lsp.json``, and ``settings.json`` into *apm_dir*."""
    import shutil

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


def _map_plugin_artifacts(
    plugin_path: Path, apm_dir: Path, manifest: dict[str, Any] | None = None
) -> None:
    """Map plugin artifacts to .apm/ subdirectories and copy pass-through files.

    Copies:
    - agents/     -> .apm/agents/
    - skills/     -> .apm/skills/
    - commands/   -> .apm/prompts/  (*.md normalised to *.prompt.md)
    - hooks/      -> .apm/hooks/    (directory, config file, or inline object)
    - .mcp.json   -> .apm/.mcp.json
    - .lsp.json   -> .apm/.lsp.json
    - settings.json -> .apm/settings.json

    Symlinks are skipped entirely to prevent content exfiltration attacks.
    Custom component paths from the manifest are security-validated to
    resolve inside *plugin_path* before any copy is attempted.

    Args:
        plugin_path: Root of the plugin directory.
        apm_dir: Path to the .apm/ directory.
        manifest: Optional plugin.json metadata; used for custom component paths.
    """
    if manifest is None:
        manifest = {}

    agent_sources = _resolve_plugin_sources(plugin_path, manifest, "agents", "agents")
    if agent_sources:
        _map_plugin_agents(agent_sources, apm_dir)

    skill_sources = _resolve_plugin_sources(plugin_path, manifest, "skills", "skills")
    if skill_sources:
        _map_plugin_skills(skill_sources, apm_dir, manifest)

    command_sources = _resolve_plugin_sources(plugin_path, manifest, "commands", "commands")
    if command_sources:
        _map_plugin_commands(command_sources, apm_dir)

    _map_plugin_hooks(manifest, plugin_path, apm_dir)

    _copy_plugin_passthrough_files(plugin_path, apm_dir)


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

    Both string entries and dict entries (e.g. ``{git: parent, path: ...}``)
    are handled.  Equality is checked with ``==`` which works correctly for
    both types.
    """
    existing = merged.setdefault(key, [])
    for entry in new_entries:
        if entry not in existing:
            existing.append(entry)


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
            with open(plugin_json, encoding="utf-8") as f:
                manifest = json.load(f)
            return bool(manifest.get("name"))
        except (OSError, json.JSONDecodeError):
            pass

    # Fallback: presence of any standard component directory
    for component_dir in ("agents", "commands", "skills", "hooks"):
        if (plugin_path / component_dir).is_dir():
            return True

    return False
