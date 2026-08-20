"""Plugin exporter -- transforms APM packages into plugin-native directories.

Produces a standalone plugin directory that Copilot CLI, Claude Code, or other
plugin hosts can consume directly.  The output contains plugin-spec artefacts
(``agents/``, ``skills/``, ``commands/``, ``plugin.json``) plus an embedded
``apm.lock.yaml`` carrying provenance metadata + a per-file SHA-256 manifest
under ``pack.bundle_files`` (issue #1098).
"""

import hashlib
import json
import re
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from ..deps.lockfile import (
    LockedDependency,
    LockFile,
    get_lockfile_path,
    migrate_lockfile_if_needed,
)
from ..models.apm_package import APMPackage, DependencyReference
from ..models.dependency.subsets import skill_subset_filter_tokens
from ..utils.archive import (
    projected_archive_path,
    validate_archive_format,
    write_tar_archive,
    write_zip_archive,
)
from ..utils.console import _rich_warning
from ..utils.path_security import PathTraversalError, ensure_path_within, safe_rmtree
from ..utils.paths import portable_relpath
from .attest import verify_attested_file
from .packer import PackResult
from .plugin_layout import PLUGIN_ROOT_DIRS, find_plugin_root_sources

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _validate_output_rel(rel: str) -> bool:
    """Return True when *rel* is safe to write inside the output directory."""
    from pathlib import PurePosixPath, PureWindowsPath

    if PurePosixPath(rel).is_absolute() or PureWindowsPath(rel).is_absolute():
        return False
    return ".." not in Path(rel).parts


_SAFE_BUNDLE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]")


def _sanitize_bundle_name(name: str) -> str:
    """Sanitize a package name/version for use as a directory component.

    Replaces path separators and traversal characters with hyphens, then
    validates the result is a single safe path component.
    """
    sanitized = _SAFE_BUNDLE_NAME_RE.sub("-", name).strip("-") or "unnamed"
    if ".." in sanitized or "/" in sanitized or "\\" in sanitized:
        sanitized = "unnamed"
    return sanitized


def _rename_prompt(name: str) -> str:
    """Strip the ``.prompt`` infix so ``foo.prompt.md`` becomes ``foo.md``."""
    if name.endswith(".prompt.md"):
        return name[: -len(".prompt.md")] + ".md"
    return name


def _normalize_bare_skill_slug(slug: str) -> str:
    """Normalize bare-skill slugs derived from dependency virtual paths."""
    normalized = slug.replace("\\", "/").strip("/")
    while normalized.startswith("skills/"):
        normalized = normalized[len("skills/") :].lstrip("/")
    if normalized == "skills":
        return ""
    return PurePosixPath(normalized).as_posix() if normalized else ""


# ---------------------------------------------------------------------------
# Component collectors
# ---------------------------------------------------------------------------


def _collect_apm_components(apm_dir: Path) -> list[tuple[Path, str]]:
    """Collect all components from a package's ``.apm/`` directory.

    Returns a list of ``(source_abs, output_rel_posix)`` tuples using the
    APM → plugin mapping table.
    """
    components: list[tuple[Path, str]] = []
    if not apm_dir.is_dir():
        return components

    # agents/ -> agents/
    _collect_flat(apm_dir / "agents", "agents", components)

    # skills/ -> skills/ (preserve sub-directory structure)
    _collect_recursive(apm_dir / "skills", "skills", components)

    # prompts/ -> commands/ (rename .prompt.md -> .md)
    _collect_recursive(apm_dir / "prompts", "commands", components, rename=_rename_prompt)

    # instructions/ -> instructions/
    _collect_recursive(apm_dir / "instructions", "instructions", components)

    # commands/ -> commands/
    _collect_recursive(apm_dir / "commands", "commands", components)

    # extensions/ -> extensions/ (canvas extensions, experimental Copilot-only).
    # Preserved verbatim so an offline bundle can carry a canvas; the files are
    # inert until the consumer enables the ``canvas`` experimental flag AND
    # approves the package via allowExecutables / ``apm approve`` at install time.
    _collect_recursive(apm_dir / "extensions", "extensions", components)

    return components


def _collect_root_plugin_components(project_root: Path) -> list[tuple[Path, str]]:
    """Collect plugin-native components authored at root level.

    Packages that already follow the plugin directory convention (``agents/``,
    ``skills/``, etc. at the repo root) have their files picked up here.
    """
    components: list[tuple[Path, str]] = []
    for dir_name in PLUGIN_ROOT_DIRS:
        if dir_name == "hooks":
            continue
        _collect_recursive(project_root / dir_name, dir_name, components)
    return components


def _emit_pack_warning(message: str, logger=None) -> None:
    """Send a pack warning through the command logger when available."""
    if logger:
        logger.warning(message)
    else:
        _rich_warning(message, symbol="warning")


def _warn_skipped_root_components(project_root: Path, logger=None) -> None:
    """Explain why plugin-native root sources are not packed."""
    for source in find_plugin_root_sources(project_root):
        if source == "hooks.json":
            message = (
                "Skipping root-level hooks.json because .apm/ is present. "
                "Move publishable hook configuration to .apm/hooks/ or remove "
                "hooks.json to silence this warning."
            )
        else:
            message = (
                f"Skipping root-level {source}/ because .apm/ is present. "
                f"Move publishable files to .apm/{source}/ or remove {source}/ "
                "to silence this warning."
            )
        _emit_pack_warning(message, logger)


def _warn_no_local_primitives(logger=None) -> None:
    """Explain how to recover from an empty APM-native source layout."""
    _emit_pack_warning(
        "No local primitives found. Expected content under .apm/. "
        "Move plugin-native content into .apm/, or remove .apm/ to restore "
        "root convention discovery.",
        logger,
    )


def _collect_bare_skill(
    install_path: Path,
    dep: "LockedDependency",
    out: list[tuple[Path, str]],
) -> None:
    """Detect a bare Claude skill (SKILL.md at dep root, no skills/ subdir).

    Bare skills are packages consisting of just ``SKILL.md`` + supporting files
    at the package root.  They have no ``.apm/`` directory or ``skills/``
    subdirectory, so the normal collectors miss them.  Map the entire package
    into ``skills/{name}/`` so the plugin host can discover it.
    """
    skill_md = install_path / "SKILL.md"
    if not skill_md.is_file():
        return
    # Already collected via .apm/skills/ or root skills/ — skip
    if any(rel.startswith("skills/") for _, rel in out):
        return
    # Derive a slug: prefer virtual_path (e.g. "frontend-design"), else last
    # segment of repo_url (e.g. "my-skill" from "owner/my-skill")
    slug = _normalize_bare_skill_slug(getattr(dep, "virtual_path", "") or "")
    if not slug:
        slug = dep.repo_url.rsplit("/", 1)[-1] if dep.repo_url else "skill"
    for f in sorted(install_path.iterdir()):
        if (
            f.is_file()
            and not f.is_symlink()
            and f.name
            not in (
                "apm.yml",
                "apm.lock.yaml",
                "plugin.json",
            )
        ):
            out.append((f, f"skills/{slug}/{f.name}"))


# -- low-level walkers -------------------------------------------------------


def _collect_flat(
    src_dir: Path,
    output_prefix: str,
    out: list[tuple[Path, str]],
    *,
    rename=None,
) -> None:
    """Add every regular non-symlink file directly inside *src_dir*."""
    if src_dir.is_symlink() or not src_dir.is_dir():
        return
    for f in sorted(src_dir.iterdir()):
        if f.is_file() and not f.is_symlink():
            name = rename(f.name) if rename else f.name
            out.append((f, f"{output_prefix}/{name}"))


def _collect_recursive(
    src_dir: Path,
    output_prefix: str,
    out: list[tuple[Path, str]],
    *,
    rename=None,
) -> None:
    """Add every regular non-symlink file under *src_dir*, preserving hierarchy."""
    if src_dir.is_symlink() or not src_dir.is_dir():
        return
    for f in sorted(src_dir.rglob("*")):
        if not f.is_file() or f.is_symlink():
            continue
        rel = f.relative_to(src_dir)
        name = rename(rel.name) if rename else rel.name
        out_rel = (rel.parent / name).as_posix()
        out.append((f, f"{output_prefix}/{out_rel}"))


# ---------------------------------------------------------------------------
# Hooks / MCP merging
# ---------------------------------------------------------------------------


_MAX_MERGE_DEPTH = 20


def _deep_merge(base: dict, overlay: dict, *, overwrite: bool = False, _depth: int = 0) -> None:
    """Recursively merge *overlay* into *base*.

    When *overwrite* is False (default), existing base keys win.
    When *overwrite* is True, overlay keys overwrite base keys.

    Raises ``ValueError`` if nesting exceeds ``_MAX_MERGE_DEPTH``.
    """
    if _depth > _MAX_MERGE_DEPTH:
        raise ValueError(f"Hooks/MCP config exceeds maximum nesting depth ({_MAX_MERGE_DEPTH})")
    for key, value in overlay.items():
        if key not in base:
            base[key] = value
        elif overwrite:
            if isinstance(base[key], dict) and isinstance(value, dict):
                _deep_merge(base[key], value, overwrite=True, _depth=_depth + 1)
            else:
                base[key] = value
        elif isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value, overwrite=False, _depth=_depth + 1)


def _collect_hooks_from_apm(apm_dir: Path) -> dict:
    """Return merged hooks from ``.apm/hooks/*.json``."""
    hooks: dict = {}
    hooks_dir = apm_dir / "hooks"
    if not hooks_dir.is_dir():
        return hooks
    for f in sorted(hooks_dir.iterdir()):
        if f.is_file() and f.suffix == ".json" and not f.is_symlink():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _deep_merge(hooks, data, overwrite=False)
            except (OSError, ValueError, RecursionError):
                # Untrusted .apm/hooks/*.json: oversized-int -> bare ValueError,
                # deep nest -> RecursionError. Fail closed (skip this file).
                pass
    return hooks


def _collect_hooks_from_root(package_root: Path) -> dict:
    """Return hooks from a root-level ``hooks.json`` or ``hooks/`` directory."""
    hooks: dict = {}
    # Single file
    hooks_file = package_root / "hooks.json"
    if hooks_file.is_file() and not hooks_file.is_symlink():
        try:
            data = json.loads(hooks_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _deep_merge(hooks, data, overwrite=False)
        except (OSError, ValueError, RecursionError):
            # Untrusted root hooks.json: fail closed (oversized-int ValueError /
            # deep-nest RecursionError are not JSONDecodeError).
            pass
    # Directory
    hooks_dir = package_root / "hooks"
    if hooks_dir.is_dir():
        for f in sorted(hooks_dir.iterdir()):
            if f.is_file() and f.suffix == ".json" and not f.is_symlink():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        _deep_merge(hooks, data, overwrite=False)
                except (OSError, ValueError, RecursionError):
                    # Untrusted .../hooks/*.json: fail closed (skip this file).
                    pass
    return hooks


def _collect_mcp(package_root: Path) -> dict:
    """Return ``mcpServers`` dict from ``.mcp.json``."""
    from ..core.plugin_manifest import collect_mcp_servers

    return collect_mcp_servers(package_root)


# ---------------------------------------------------------------------------
# devDependencies filtering
# ---------------------------------------------------------------------------


def _get_dev_dependency_urls(apm_yml_path: Path) -> set[tuple[str, str]]:
    """Read ``devDependencies.apm`` from raw YAML and return a set of
    ``(repo_url, virtual_path)`` tuples for matching against lockfile entries.

    Using the composite key avoids false positives when multiple virtual
    packages share the same base repo (e.g. different sub-paths under
    ``github/awesome-copilot``).
    """
    try:
        from ..utils.yaml_io import load_yaml

        data = load_yaml(apm_yml_path)
    except (yaml.YAMLError, OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    dev_deps = data.get("devDependencies", {})
    if not isinstance(dev_deps, dict):
        return set()
    apm_dev = dev_deps.get("apm", [])
    if not isinstance(apm_dev, list):
        return set()
    keys: set[tuple[str, str]] = set()
    for dep in apm_dev:
        if isinstance(dep, str):
            try:
                ref = DependencyReference.parse(dep)
                keys.add((ref.repo_url, ref.virtual_path or ""))
            except ValueError:
                keys.add((dep, ""))
        elif isinstance(dep, dict):
            try:
                ref = DependencyReference.parse_from_dict(dep)
                keys.add((ref.repo_url, ref.virtual_path or ""))
            except ValueError:
                pass
    return keys


# ---------------------------------------------------------------------------
# Plugin.json helpers
# ---------------------------------------------------------------------------


def _find_or_synthesize_plugin_json(
    project_root: Path,
    apm_yml_path: Path,
    *,
    suppress_missing_warning: bool = False,
    logger=None,
) -> dict:
    """Locate an existing ``plugin.json`` or synthesise one from ``apm.yml``."""
    from ..core.plugin_manifest import find_or_synthesize_plugin_json

    return find_or_synthesize_plugin_json(
        project_root,
        apm_yml_path,
        suppress_missing_warning=suppress_missing_warning,
        logger=logger,
    )


def _has_marketplace_block(apm_yml_path: Path) -> bool:
    """Return True if apm.yml declares a non-empty ``marketplace:`` block."""
    try:
        import yaml

        # Bounded loader so a hostile apm.yml cannot wedge the export with a
        # merge/alias expansion bomb (fails closed as yaml.YAMLError).
        from apm_cli.utils.yaml_io import load_yaml

        data = load_yaml(apm_yml_path) or {}
    except (OSError, yaml.YAMLError):
        return False
    return bool(data.get("marketplace"))


def _update_plugin_json_paths(plugin_json: dict, output_files: list[str], logger=None) -> dict:
    r"""Strip component-path keys from ``plugin.json``.

    Per the official Claude Code plugin manifest schema, the
    ``agents``/``skills``/``commands`` keys point to *additional* files
    OUTSIDE the convention directories (``agents/``, ``skills/``,
    ``commands/``) and each entry must match ``^\./.*`` (relative path)
    and the per-key file-extension pattern. The ``instructions`` key is
    not defined by the schema at all. The convention directories
    themselves are auto-discovered by Claude Code -- listing them here
    is invalid (or unrecognized).

    APM emits everything into the convention directories, so we drop
    these keys entirely to keep the manifest schema-conformant.

    The ``output_files`` argument is retained for signature stability
    (and as a hook for future "additional files" extensions); it is
    currently unused.
    """
    result = dict(plugin_json)
    stripped = [k for k in ("agents", "skills", "commands", "instructions") if k in result]
    for key in stripped:
        result.pop(key, None)
    if stripped:
        msg = (
            "Stripped schema-invalid keys from authored plugin.json: "
            f"{', '.join(stripped)} -- convention directories are auto-discovered by Claude Code"
        )
        if logger:
            logger.warning(msg)
        else:
            _rich_warning(msg)
    return result


# ---------------------------------------------------------------------------
# Dep → filesystem helpers
# ---------------------------------------------------------------------------


def _dep_install_path(dep: LockedDependency, apm_modules_dir: Path) -> Path:
    """Compute the filesystem install path for a locked dependency."""
    dep_ref = dep.to_dependency_ref()
    return dep_ref.get_install_path(apm_modules_dir)


def _deployed_path_parts(rel_path: str) -> tuple[str, ...]:
    """Return safe POSIX path parts for a lockfile deployed_files entry."""
    rel = rel_path.replace("\\", "/")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or PureWindowsPath(rel).is_absolute():
        raise ValueError(f"Refusing to pack absolute deployed file path: {rel_path!r}")
    parts = pure.parts
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"Refusing to pack unsafe deployed file path: {rel_path!r}")
    return parts


def _skill_name_from_deployed_parts(parts: tuple[str, ...]) -> str | None:
    """Return the deployed skill name when *parts* point inside a skills dir."""
    if len(parts) >= 3 and parts[0].startswith(".") and parts[1] == "skills":
        return parts[2]
    if len(parts) >= 2 and parts[0] == "skills":
        return parts[1]
    return None


def _plugin_rel_for_deployed_path(
    rel_path: str,
    skill_subset: set[str] | None,
) -> str | None:
    """Map an installed deployed_files path back to plugin-native output layout."""
    parts = _deployed_path_parts(rel_path)
    if not parts:
        return None

    if parts[0] == "skills":
        skill_name = _skill_name_from_deployed_parts(parts)
        if skill_subset and (skill_name is None or skill_name not in skill_subset):
            return None
        plugin_parts = list(parts)
    elif parts[0] in {"agents", "commands", "instructions", "extensions"}:
        plugin_parts = list(parts)
    elif len(parts) >= 3 and parts[0].startswith("."):
        skill_name = _skill_name_from_deployed_parts(parts)
        if skill_name is not None:
            if skill_subset and skill_name not in skill_subset:
                return None
            plugin_parts = ["skills", *parts[2:]]
        elif parts[1] == "agents":
            plugin_parts = ["agents", *parts[2:]]
        elif parts[1] in {"commands", "prompts"}:
            plugin_parts = ["commands", *parts[2:]]
        elif parts[1] in {"instructions", "rules", "steering"}:
            plugin_parts = ["instructions", *parts[2:]]
        elif parts[1] == "extensions":
            plugin_parts = ["extensions", *parts[2:]]
        elif parts[1] == "hooks":
            plugin_parts = ["hooks", *parts[2:]]
        else:
            return None
    elif len(parts) >= 2 and parts[0].startswith(".") and parts[1] == "hooks.json":
        plugin_parts = ["hooks.json"]
    else:
        return None

    if plugin_parts[0] == "commands" and plugin_parts[-1].endswith(".prompt.md"):
        plugin_parts[-1] = _rename_prompt(plugin_parts[-1])
    return PurePosixPath(*plugin_parts).as_posix()


def _collect_explicit_local_components(
    project_root: Path,
    includes: list[str],
) -> tuple[list[tuple[Path, str]], dict]:
    """Collect only explicitly listed local paths and validate each path."""
    components: list[tuple[Path, str]] = []
    hooks: dict = {}
    for declared_path in includes:
        parts = _deployed_path_parts(declared_path)
        candidate = project_root.joinpath(*parts)
        if candidate.is_symlink():
            raise ValueError(
                f"includes path {declared_path!r} is a symlink. "
                "Replace it with a regular file or directory."
            )
        try:
            source = ensure_path_within(candidate, project_root)
        except PathTraversalError as exc:
            raise ValueError(
                f"includes path {declared_path!r} escapes the project root. "
                "Fix the path in apm.yml."
            ) from exc
        if not source.exists():
            raise ValueError(
                f"includes path {declared_path!r} does not exist. "
                "Fix the path in apm.yml or create it."
            )
        entries = [source] if source.is_file() else sorted(source.rglob("*"))
        for entry in entries:
            if entry.is_symlink():
                raise ValueError(
                    f"Symlink found inside includes path {declared_path!r}: "
                    f"{entry.name}. Remove the symlink or list a regular path."
                )
        for file_path in (entry for entry in entries if entry.is_file()):
            try:
                file_path = ensure_path_within(file_path, project_root)
            except PathTraversalError as exc:
                raise ValueError(
                    f"A file inside includes path {declared_path!r} escapes the "
                    "project root. Remove the symlink or fix the path in apm.yml."
                ) from exc
            repo_relative = portable_relpath(file_path, project_root)
            is_hook = (
                repo_relative == "hooks.json"
                or repo_relative.startswith("hooks/")
                or repo_relative.startswith(".apm/hooks/")
            )
            if is_hook:
                try:
                    hook_data = json.loads(file_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, RecursionError) as exc:
                    raise ValueError(
                        f"Explicit hook include is not valid JSON: {repo_relative}"
                    ) from exc
                if not isinstance(hook_data, dict):
                    raise ValueError(
                        f"Explicit hook include must contain a JSON object: {repo_relative}"
                    )
                _deep_merge(hooks, hook_data, overwrite=False)
                continue
            plugin_relative = _plugin_rel_for_deployed_path(repo_relative, None)
            if plugin_relative is None:
                raise ValueError(
                    f"Explicit include path is not a packable primitive: {repo_relative}"
                )
            components.append((file_path, plugin_relative))
    return components, hooks


def _verify_attested_hash(
    source: Path,
    project_root: Path,
    dep: LockedDependency,
) -> None:
    """Fail loudly when a packed deployed file diverges from its attested hash.

    ``deployed_file_hashes`` records the SHA-256 (``"sha256:<hex>"``) of each
    file at install time. Verifying the on-disk copy before packing closes the
    integrity half of the provenance hole: a deployed file that was tampered
    or corrupted after ``apm install`` must never enter the bundle silently.

    Files with no recorded hash (older lockfiles predating
    ``deployed_file_hashes``) are packed without verification -- absence of an
    attestation is forward-compat tolerated, presence of a *mismatched* one is
    a hard error. Delegates to the shared :func:`verify_attested_file` helper so
    the plugin and archive pack paths share one implementation.
    """
    rel = source.relative_to(project_root).as_posix()
    expected = dep.deployed_file_hashes.get(rel) if dep.deployed_file_hashes else None
    verify_attested_file(source, expected, dep.repo_url, rel)


def _append_deployed_component(
    components: list[tuple[Path, str]],
    source: Path,
    output_rel: str,
    seen_outputs: set[str],
    project_root: Path,
    dep: LockedDependency,
) -> None:
    """Append one deployed component unless it is unsafe or already mapped."""
    if output_rel in seen_outputs or not _validate_output_rel(output_rel):
        return
    if source.is_file() and not source.is_symlink():
        _verify_attested_hash(source, project_root, dep)
        components.append((source, output_rel))
        seen_outputs.add(output_rel)


def _collect_deployed_components(
    project_root: Path,
    dep: LockedDependency,
) -> list[tuple[Path, str]]:
    """Collect dependency components from lockfile deployed_files entries."""
    components: list[tuple[Path, str]] = []
    missing: list[str] = []
    seen_outputs: set[str] = set()
    skill_subset = skill_subset_filter_tokens(dep.skill_subset)

    for rel_path in dep.deployed_files:
        try:
            plugin_rel = _plugin_rel_for_deployed_path(rel_path, skill_subset)
        except ValueError as exc:
            raise ValueError(f"Cannot pack dependency {dep.repo_url}: {exc}") from exc
        if plugin_rel is None:
            continue
        source = project_root / rel_path
        try:
            source = ensure_path_within(source, project_root)
        except PathTraversalError as exc:
            raise ValueError(
                f"Cannot pack dependency {dep.repo_url}: deployed file path "
                f"escapes the project root: {rel_path!r}"
            ) from exc
        if not source.exists():
            missing.append(rel_path)
            continue
        if source.is_symlink():
            continue
        if source.is_dir():
            for child in sorted(source.rglob("*")):
                if not child.is_file() or child.is_symlink():
                    continue
                # Defense-in-depth: rglob's symlink-following behaviour is
                # Python-version dependent, so re-assert containment on every
                # expanded child rather than trusting the walk. A planted
                # directory symlink whose target escapes ``project_root`` is
                # rejected here even if an intermediate component was a symlink.
                try:
                    ensure_path_within(child, project_root)
                except PathTraversalError:
                    continue
                child_rel = child.relative_to(source).as_posix()
                child_output = (PurePosixPath(plugin_rel) / child_rel).as_posix()
                _append_deployed_component(
                    components, child, child_output, seen_outputs, project_root, dep
                )
        else:
            _append_deployed_component(
                components, source, plugin_rel, seen_outputs, project_root, dep
            )

    if missing:
        shown_missing = missing[:10]
        remaining = len(missing) - len(shown_missing)
        suffix = f"\n  ... and {remaining} more" if remaining else ""
        raise ValueError(
            f"Cannot pack dependency {dep.repo_url}: installed files recorded "
            "in apm.lock.yaml are missing on disk. Run 'apm install' to "
            "restore them, then pack again:\n"
            + "\n".join(f"  - {path}" for path in shown_missing)
            + suffix
        )
    return components


def _cache_would_contribute_primitives(install_path: Path, dep: LockedDependency) -> bool:
    """Return True if the unattested apm_modules cache holds packable primitives.

    Used ONLY to decide whether to fail loudly when a locked dependency records
    no ``deployed_files``. This reads the cache to DETECT a provenance gap; it
    never copies cache bytes into the bundle. A dependency that installed
    real components but recorded none of them in the lockfile is a stale or
    partial install -- packing its cache would leak unattested content, so we
    refuse rather than silently pack it.
    """
    if not install_path.is_dir():
        return False
    probe = _collect_apm_components(install_path / ".apm")
    probe.extend(_collect_root_plugin_components(install_path))
    _collect_bare_skill(install_path, dep, probe)
    return bool(probe)


def _cache_would_contribute_hooks_or_mcp(install_path: Path) -> bool:
    """Return True if the unattested cache holds hooks-config or MCP-config.

    Hooks/MCP *configuration* (``.apm/hooks/*.json``, root ``hooks.json`` /
    ``hooks/``, ``.mcp.json``) is merged into shared host settings by
    ``apm install`` and is never recorded in the lockfile ``deployed_files``.
    Because plugin pack now emits only lockfile-attested content, such config
    is dropped from the bundle. This probe drives a transition warning that
    names the dependency whose cached config will NOT be packed; it never
    copies cache bytes. Hook *scripts* recorded in ``deployed_files`` are
    unaffected and still pack.
    """
    if not install_path.is_dir():
        return False
    if _collect_mcp(install_path):
        return True
    if _collect_hooks_from_apm(install_path / ".apm"):
        return True
    return bool(_collect_hooks_from_root(install_path))


# ---------------------------------------------------------------------------
# Main exporter
# ---------------------------------------------------------------------------


def export_plugin_bundle(
    project_root: Path,
    output_dir: Path,
    target: str | None = None,
    archive: bool = False,
    archive_format: str = "zip",
    dry_run: bool = False,
    force: bool = False,
    logger=None,
) -> PackResult:
    """Export the project as a plugin-native directory.

    The output contains only plugin-spec artefacts (``agents/``, ``skills/``,
    ``commands/``, ``plugin.json``, …) with no APM-specific files.

    Args:
        project_root: Root of the project containing ``apm.yml``.
        output_dir: Parent directory for the generated bundle.
        target: Unused for plugin format (reserved for future use).
        archive: If True, produce a ``.zip`` (or ``.tar.gz`` when *archive_format* is ``"tar.gz"``) and remove the directory.
        archive_format: Archive format when *archive* is True -- ``"zip"`` (default) or ``"tar.gz"``.
        dry_run: If True, resolve the file list without writing to disk.
        force: On collision, last writer wins instead of first.

    Returns:
        :class:`PackResult` describing what was produced.
    """
    # 1. Read lockfile
    migrate_lockfile_if_needed(project_root)
    lockfile_path = get_lockfile_path(project_root)
    lockfile = LockFile.read(lockfile_path)

    # 2. Read apm.yml
    apm_yml_path = project_root / "apm.yml"
    package = APMPackage.from_apm_yml(apm_yml_path)
    pkg_name = package.name
    pkg_version = package.version or "0.0.0"

    # Guard: reject local-path dependencies (non-portable)
    for dep_ref in package.get_apm_dependencies():
        if dep_ref.is_local:
            raise ValueError(
                f"Cannot pack — apm.yml contains local path dependency: "
                f"{dep_ref.local_path}\n"
                f"Local dependencies are for development only. Replace them with "
                f"remote references (e.g., 'owner/repo') before packing."
            )

    # 3. Find or synthesize plugin.json
    plugin_json = _find_or_synthesize_plugin_json(
        project_root,
        apm_yml_path,
        suppress_missing_warning=_has_marketplace_block(apm_yml_path),
        logger=logger,
    )

    # 4. devDependencies filtering
    dev_dep_urls = _get_dev_dependency_urls(apm_yml_path)

    # 5. Collect components -- deps first (lockfile order), then root package
    #    file_map: output_rel_posix -> (source_abs, owner_name)
    file_map: dict[str, tuple[Path, str]] = {}
    collisions: list[str] = []
    merged_hooks: dict = {}
    merged_mcp: dict = {}

    apm_modules_dir = project_root / "apm_modules"

    if lockfile:
        for dep in lockfile.get_all_dependencies():
            # Prefer lockfile is_dev flag (covers transitive deps);
            # fall back to apm.yml URL matching for older lockfiles
            if (
                getattr(dep, "is_dev", False)
                or (dep.repo_url, getattr(dep, "virtual_path", "") or "") in dev_dep_urls
            ):
                continue

            dep_name = dep.repo_url

            # Provenance rule (issue #1999 follow-up): a dependency's content
            # is packed EXCLUSIVELY from what the lockfile attests via
            # ``deployed_files`` (+ per-file ``deployed_file_hashes``).
            # ``apm_modules`` is an unattested cache -- it carries no integrity
            # or provenance guarantee, so no bytes from it may reach the bundle.
            install_path = _dep_install_path(dep, apm_modules_dir)
            if dep.deployed_files:
                dep_components = _collect_deployed_components(project_root, dep)
                if dep.skill_subset and not dep_components:
                    declared_skills = ", ".join(dep.skill_subset)
                    raise ValueError(
                        f"Cannot pack dependency {dep.repo_url}: the skills "
                        f"recorded in apm.lock.yaml (skill_subset: {declared_skills}) "
                        "were not found among its installed files. Run 'apm install' "
                        "to re-deploy the expected skills, then pack again."
                    )
            elif _cache_would_contribute_primitives(install_path, dep):
                # Unattested primitives sit in the cache but the lockfile
                # recorded none of them: a stale or partial install. Refuse
                # to pack the cache -- fail loudly with a fix.
                raise ValueError(
                    f"Cannot pack dependency {dep.repo_url}: the lockfile records "
                    "no deployed files for it, but installed content that cannot "
                    "be verified exists in the apm_modules cache (a stale or "
                    "partial install). Run 'apm install' to record provenance in "
                    "apm.lock.yaml, then pack again."
                )
            else:
                # No attested content and nothing packable in the cache: the
                # dependency contributes no plugin primitives (e.g. an
                # MCP-only or hooks-config-only package). Skip it cleanly.
                dep_components = []

            # Transition warning (#2013): dependency hooks/MCP *config* lives in
            # the unattested cache and is never packed. Name the dependency so
            # an author who relied on it merging into shared settings is not
            # surprised by the silent exclusion. Attested primitives are packed
            # regardless of this warning.
            if _cache_would_contribute_hooks_or_mcp(install_path):
                _warn = (
                    f"dependency {dep.repo_url} contributed hooks/MCP config that "
                    "is not attested in apm.lock.yaml; it will NOT be packed. "
                    "Attested primitives (skills/agents/etc.) are unaffected."
                )
                if logger:
                    logger.warning(_warn)
                else:
                    _rich_warning(_warn, symbol="warning")

            _merge_file_map(file_map, dep_components, dep_name, force, collisions)

    # 6. Collect own components according to the local source authority.
    own_apm_dir = project_root / ".apm"
    if isinstance(package.includes, list):
        own_components, root_hooks = _collect_explicit_local_components(
            project_root,
            package.includes,
        )
    else:
        own_components = _collect_apm_components(own_apm_dir)
        root_hooks = _collect_hooks_from_apm(own_apm_dir)
        if own_apm_dir.is_dir():
            _warn_skipped_root_components(project_root, logger)
        else:
            own_components.extend(_collect_root_plugin_components(project_root))
            root_hooks_top = _collect_hooks_from_root(project_root)
            _deep_merge(root_hooks, root_hooks_top, overwrite=False)

    if (
        not isinstance(package.includes, list)
        and own_apm_dir.is_dir()
        and not own_components
        and not root_hooks
    ):
        _warn_no_local_primitives(logger)

    _merge_file_map(file_map, own_components, pkg_name, force, collisions)

    # Hooks -- root package wins on key collision
    _deep_merge(merged_hooks, root_hooks, overwrite=True)

    # MCP -- root package wins on server-name collision
    root_mcp = _collect_mcp(project_root)
    _deep_merge(merged_mcp, root_mcp, overwrite=True)

    # 7. Emit collision warnings
    for msg in collisions:
        if logger:
            logger.warning(msg)
        else:
            _rich_warning(msg)

    # 8. Build output file list (sorted for determinism)
    output_files = sorted(file_map.keys())

    # Add generated files to the list
    if merged_hooks:
        output_files.append("hooks.json")
    if merged_mcp:
        output_files.append(".mcp.json")
    output_files.append("plugin.json")

    # 9. Dry run -- return file list without writing
    safe_name = _sanitize_bundle_name(pkg_name)
    safe_version = _sanitize_bundle_name(pkg_version)
    bundle_dir = output_dir / f"{safe_name}-{safe_version}"
    ensure_path_within(bundle_dir, output_dir)
    if dry_run:
        bundle_path = (
            projected_archive_path(output_dir, bundle_dir.name, archive_format)
            if archive
            else bundle_dir
        )
        return PackResult(bundle_path=bundle_path, files=output_files)

    # 10. Security scan (warn-only, never blocks)
    from ..security.gate import WARN_POLICY, SecurityGate

    scan_findings_total = 0
    for _rel, (src, _owner) in file_map.items():
        if src.is_symlink():
            continue
        if src.is_dir():
            verdict = SecurityGate.scan_files(src, policy=WARN_POLICY)
            scan_findings_total += len(verdict.all_findings)
        elif src.is_file():
            try:
                text = src.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            verdict = SecurityGate.scan_text(text, str(src), policy=WARN_POLICY)
            scan_findings_total += len(verdict.all_findings)
    if scan_findings_total:
        _warn_msg = (
            f"Bundle contains {scan_findings_total} hidden character(s) across "
            f"source files — run 'apm audit' to inspect before publishing"
        )
        if logger:
            logger.warning(_warn_msg)
        else:
            _rich_warning(_warn_msg)

    # 11. Write files to output directory (clean slate to prevent symlink attacks)
    if bundle_dir.exists():
        safe_rmtree(bundle_dir, output_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    for output_rel, (source_abs, _owner) in file_map.items():
        if not _validate_output_rel(output_rel):
            continue
        dest = bundle_dir / output_rel
        if source_abs.is_symlink():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            ensure_path_within(dest, bundle_dir)
        except PathTraversalError:
            continue
        shutil.copy2(source_abs, dest, follow_symlinks=False)

    # 12. Write merged hooks.json
    if merged_hooks:
        (bundle_dir / "hooks.json").write_text(
            json.dumps(merged_hooks, indent=2, sort_keys=True), encoding="utf-8"
        )

    # 13. Write merged .mcp.json
    if merged_mcp:
        (bundle_dir / ".mcp.json").write_text(
            json.dumps({"mcpServers": merged_mcp}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # 14. Write plugin.json with updated component paths
    plugin_json = _update_plugin_json_paths(plugin_json, output_files, logger=logger)
    (bundle_dir / "plugin.json").write_text(
        json.dumps(plugin_json, indent=2, sort_keys=False), encoding="utf-8"
    )

    # 14b. Write enriched lockfile with bundle_files manifest (issue #1098).
    # Walk the bundle and hash every file (excluding the lockfile itself,
    # which we are about to write) so install-time integrity verification can
    # detect tampering without needing the original deployed_files map.
    if lockfile is not None:
        from .lockfile_enrichment import enrich_lockfile_for_pack

        bundle_files: dict[str, str] = {}
        for fp in bundle_dir.rglob("*"):
            if not fp.is_file() or fp.is_symlink():
                continue
            rel = fp.relative_to(bundle_dir).as_posix()
            if rel == "apm.lock.yaml":
                continue
            bundle_files[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()
        # Issue #1207 D1: do NOT silently substitute ``"copilot"`` when
        # ``target`` is missing.  Bundles are target-agnostic at install
        # time; ``pack.target`` is recorded as informational metadata only.
        # Falling back to ``"all"`` preserves the lockfile-filter shape
        # (which uses target prefixes to narrow each dep's deployed_files
        # list to the union of supported targets) without locking the
        # bundle to a single client.
        enriched_yaml = enrich_lockfile_for_pack(
            lockfile,
            "plugin",
            target or "all",
            bundle_files=bundle_files,
        )
        (bundle_dir / "apm.lock.yaml").write_text(enriched_yaml, encoding="utf-8")

    result = PackResult(bundle_path=bundle_dir, files=output_files)

    # 15. Archive if requested
    if archive:
        validate_archive_format(archive_format)
        archive_path = projected_archive_path(output_dir, bundle_dir.name, archive_format)
        if archive_format == "tar.gz":
            write_tar_archive(bundle_dir, archive_path)
        else:
            write_zip_archive(bundle_dir, archive_path)
        shutil.rmtree(bundle_dir)
        result.bundle_path = archive_path

    return result


# ---------------------------------------------------------------------------
# Collision handling
# ---------------------------------------------------------------------------


def _merge_file_map(
    file_map: dict[str, tuple[Path, str]],
    components: list[tuple[Path, str]],
    owner: str,
    force: bool,
    collisions: list[str],
) -> None:
    """Merge *components* into *file_map* with collision handling.

    Without ``--force``: first writer wins (skip with warning).
    With ``--force``: last writer wins (overwrite with warning).
    """
    for source, output_rel in components:
        if not _validate_output_rel(output_rel):
            continue
        if output_rel in file_map:
            existing_owner = file_map[output_rel][1]
            collisions.append(
                f"{output_rel} — collision between '{existing_owner}' and "
                f"'{owner}' ({'last writer wins' if force else 'first writer wins'})"
            )
            if force:
                file_map[output_rel] = (source, owner)
            # else: first writer wins, skip
        else:
            file_map[output_rel] = (source, owner)
