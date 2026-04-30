"""Plugin exporter -- transforms APM packages into plugin-native directories.

Produces a standalone plugin directory that Copilot CLI, Claude Code, or other
plugin hosts can consume directly.  The output contains no APM-specific files
(no ``apm.yml``, ``apm_modules/``, ``.apm/``, or ``apm.lock.yaml``).
"""

import json
import os  # noqa: F401
import re
import shutil
import tarfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple  # noqa: F401, UP035

import yaml

from ..deps.lockfile import (
    LockedDependency,
    LockFile,
    get_lockfile_path,
    migrate_lockfile_if_needed,
)
from ..models.apm_package import APMPackage, DependencyReference
from ..utils.console import _rich_info, _rich_warning  # noqa: F401
from ..utils.path_security import PathTraversalError, ensure_path_within, safe_rmtree
from .packer import PackResult

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

    return components


def _collect_root_plugin_components(project_root: Path) -> list[tuple[Path, str]]:
    """Collect plugin-native components authored at root level.

    Packages that already follow the plugin directory convention (``agents/``,
    ``skills/``, etc. at the repo root) have their files picked up here.
    """
    components: list[tuple[Path, str]] = []
    for dir_name in ("agents", "skills", "commands", "instructions"):
        _collect_recursive(project_root / dir_name, dir_name, components)
    return components


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
    if not src_dir.is_dir():
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
    if not src_dir.is_dir():
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
            except (json.JSONDecodeError, OSError):
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
        except (json.JSONDecodeError, OSError):
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
                except (json.JSONDecodeError, OSError):
                    pass
    return hooks


def _collect_mcp(package_root: Path) -> dict:
    """Return ``mcpServers`` dict from ``.mcp.json``."""
    mcp_file = package_root / ".mcp.json"
    if not mcp_file.is_file() or mcp_file.is_symlink():
        return {}
    try:
        data = json.loads(mcp_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            servers = data.get("mcpServers", {})
            return dict(servers) if isinstance(servers, dict) else {}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


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
    logger=None,
) -> dict:
    """Locate an existing ``plugin.json`` or synthesise one from ``apm.yml``."""
    from ..deps.plugin_parser import synthesize_plugin_json_from_apm_yml
    from ..utils.helpers import find_plugin_json

    plugin_json_path = find_plugin_json(project_root)
    if plugin_json_path is not None:
        try:
            return json.loads(plugin_json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            _warn_msg = (
                f"Found plugin.json at {plugin_json_path} but could not parse it: {exc}. "
                "Falling back to synthesis from apm.yml."
            )
            if logger:
                logger.warning(_warn_msg)
            else:
                _rich_warning(_warn_msg)

    else:
        _warn_msg = (
            "No plugin.json found. Synthesizing from apm.yml. Consider running 'apm init --plugin'."
        )
        if logger:
            logger.warning(_warn_msg)
        else:
            _rich_warning(_warn_msg)
    return synthesize_plugin_json_from_apm_yml(apm_yml_path)


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


# ---------------------------------------------------------------------------
# Main exporter
# ---------------------------------------------------------------------------


def export_plugin_bundle(
    project_root: Path,
    output_dir: Path,
    target: str | None = None,
    archive: bool = False,
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
        archive: If True, produce a ``.tar.gz`` and remove the directory.
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
    plugin_json = _find_or_synthesize_plugin_json(project_root, apm_yml_path, logger=logger)

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

            install_path = _dep_install_path(dep, apm_modules_dir)
            if not install_path.is_dir():
                continue

            dep_name = dep.repo_url

            # Collect from .apm/
            dep_apm_dir = install_path / ".apm"
            dep_components = _collect_apm_components(dep_apm_dir)

            # Also collect root-level plugin-native dirs from the dep
            dep_components.extend(_collect_root_plugin_components(install_path))

            # Bare Claude skills: SKILL.md at dep root with no skills/ subdir
            _collect_bare_skill(install_path, dep, dep_components)

            _merge_file_map(file_map, dep_components, dep_name, force, collisions)

            # Hooks -- deps merge (first wins among deps)
            dep_hooks = _collect_hooks_from_apm(dep_apm_dir)
            dep_hooks_root = _collect_hooks_from_root(install_path)
            _deep_merge(dep_hooks, dep_hooks_root, overwrite=False)
            _deep_merge(merged_hooks, dep_hooks, overwrite=False)

            # MCP -- deps merge (first wins among deps)
            dep_mcp = _collect_mcp(install_path)
            _deep_merge(merged_mcp, dep_mcp, overwrite=False)

    # 6. Collect own components (.apm/ and root-level)
    own_apm_dir = project_root / ".apm"
    own_components = _collect_apm_components(own_apm_dir)
    own_components.extend(_collect_root_plugin_components(project_root))
    _merge_file_map(file_map, own_components, pkg_name, force, collisions)

    # Hooks -- root package wins on key collision
    root_hooks = _collect_hooks_from_apm(own_apm_dir)
    root_hooks_top = _collect_hooks_from_root(project_root)
    _deep_merge(root_hooks, root_hooks_top, overwrite=False)
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
        return PackResult(bundle_path=bundle_dir, files=output_files)

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

    result = PackResult(bundle_path=bundle_dir, files=output_files)

    # 15. Archive if requested
    if archive:
        archive_path = output_dir / f"{bundle_dir.name}.tar.gz"
        ensure_path_within(archive_path, output_dir)
        with tarfile.open(archive_path, "w:gz") as tar:

            def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
                if info.issym() or info.islnk():
                    return None  # reject symlinks injected after write
                return info

            tar.add(bundle_dir, arcname=bundle_dir.name, filter=_tar_filter)
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
