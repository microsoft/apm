"""Canonical ownership helpers for merged native hook configuration."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)


def reinject_apm_source_from_sidecar(hooks: dict, sidecar_data: dict) -> None:
    """Restore sidecar ownership markers onto matching in-memory entries."""
    for event_name, sidecar_entries in sidecar_data.items():
        if event_name not in hooks or not isinstance(sidecar_entries, list):
            continue
        pool: dict[str, deque[str]] = {}
        for sidecar_entry in sidecar_entries:
            if isinstance(sidecar_entry, dict) and "_apm_source" in sidecar_entry:
                comparable = {
                    key: value
                    for key, value in sorted(sidecar_entry.items())
                    if key != "_apm_source"
                }
                content_key = json.dumps(comparable, sort_keys=True)
                pool.setdefault(content_key, deque()).append(sidecar_entry["_apm_source"])

        for disk_entry in hooks[event_name]:
            if not isinstance(disk_entry, dict) or "_apm_source" in disk_entry:
                continue
            comparable = {
                key: value for key, value in sorted(disk_entry.items()) if key != "_apm_source"
            }
            content_key = json.dumps(comparable, sort_keys=True)
            sources = pool.get(content_key)
            if sources:
                disk_entry["_apm_source"] = sources.popleft()
                if not sources:
                    del pool[content_key]


def extract_apm_source_sidecar(hooks: dict) -> dict[str, list[dict[str, Any]]]:
    """Extract ownership metadata and leave native hook entries schema-clean."""
    sidecar: dict[str, list[dict[str, Any]]] = {}
    for event_name, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        owned = [
            dict(entry) for entry in entries if isinstance(entry, dict) and "_apm_source" in entry
        ]
        if owned:
            sidecar[event_name] = owned
        for entry in entries:
            if isinstance(entry, dict):
                entry.pop("_apm_source", None)
    return sidecar


def dependency_hook_source_marker(dependency_ref) -> str:
    """Project dependency identity into a collision-resistant sidecar marker."""
    if dependency_ref.is_local:
        modules_root = Path("apm_modules")
        return dependency_ref.get_install_path(modules_root).relative_to(modules_root).as_posix()
    return dependency_ref.get_canonical_dependency_string()


def lockfile_dependency_identities(
    project_root: Path,
) -> tuple[list[tuple[str, str]], bool]:
    """Return install path and hook marker from a readable lockfile."""
    try:
        from apm_cli.deps.lockfile import LEGACY_LOCKFILE_NAME, LockFile, get_lockfile_path

        lockfile_path = get_lockfile_path(project_root)
        if not lockfile_path.exists():
            legacy_path = project_root / LEGACY_LOCKFILE_NAME
            if legacy_path.exists():
                lockfile_path = legacy_path
        if not lockfile_path.exists():
            return [], False
        lockfile = LockFile.read(lockfile_path)
        if lockfile is None:
            return [], False
        identities: list[tuple[str, str]] = []
        apm_modules = project_root / "apm_modules"
        for locked_dependency in lockfile.get_all_dependencies():
            dependency = locked_dependency.to_dependency_ref()
            identities.append(
                (
                    dependency.get_install_path(apm_modules).relative_to(apm_modules).as_posix(),
                    dependency_hook_source_marker(dependency),
                )
            )
        return identities, True
    except (AttributeError, OSError, TypeError, ValueError, KeyError):
        return [], False


def safe_dependency_path(apm_modules: Path, relative_path: str) -> Path | None:
    """Return a lockfile dependency path without escaping apm_modules."""
    try:
        validate_path_segments(
            relative_path,
            context="lockfile dependency path",
            reject_empty=True,
        )
        package_path = apm_modules / Path(relative_path)
        ensure_path_within(package_path, apm_modules)
        if has_symlink_component(apm_modules, package_path):
            return None
        return package_path
    except (OSError, PathTraversalError, RuntimeError, TypeError):
        return None


def has_symlink_component(apm_modules: Path, package_path: Path) -> bool:
    """Return True when any component below apm_modules is a symlink."""
    try:
        relative = package_path.relative_to(apm_modules)
        current = apm_modules
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return True
        return False
    except (OSError, ValueError):
        return True


def is_dependency_package_dir(path: Path) -> bool:
    """Return True when a path looks like an installed package root."""
    try:
        hooks = path / "hooks"
        apm_hooks = path / ".apm" / "hooks"
        apm_yml = path / "apm.yml"
        skill_md = path / "SKILL.md"
        return (
            (hooks.is_dir() and not hooks.is_symlink())
            or (apm_hooks.is_dir() and not apm_hooks.is_symlink())
            or (apm_yml.is_file() and not apm_yml.is_symlink())
            or (skill_md.is_file() and not skill_md.is_symlink())
        )
    except OSError:
        return False


def add_dependency_source(
    sources: set[str],
    package_path: Path,
    apm_modules: Path,
) -> bool:
    """Add legacy leaf and canonical install-path markers for one package."""
    try:
        if (
            not package_path.is_dir()
            or package_path.is_symlink()
            or not is_dependency_package_dir(package_path)
        ):
            return False
        sources.add(package_path.name)
        sources.add(package_path.relative_to(apm_modules).as_posix())
        return True
    except (OSError, ValueError):
        return False


def child_dependency_dirs(path: Path) -> list[Path]:
    """Return direct non-hidden child dirs without following symlink roots."""
    try:
        if path.is_symlink() or not path.is_dir():
            return []
        return sorted(
            [
                child
                for child in path.iterdir()
                if not child.is_symlink() and child.is_dir() and not child.name.startswith(".")
            ],
            key=lambda child: child.name,
        )
    except OSError:
        return []


def collect_known_subdirectory_sources(
    sources: set[str],
    repo_root: Path,
    apm_modules: Path,
) -> None:
    """Collect sources from known virtual subdirectory layouts."""
    for namespace in ("collections", "skills"):
        for package_path in child_dependency_dirs(repo_root / namespace):
            add_dependency_source(sources, package_path, apm_modules)

    apm_dir = repo_root / ".apm"
    try:
        if apm_dir.is_symlink() or not apm_dir.is_dir():
            return
    except OSError:
        return
    for primitive in ("agents", "commands", "hooks", "instructions", "prompts", "skills"):
        for package_path in child_dependency_dirs(apm_dir / primitive):
            add_dependency_source(sources, package_path, apm_modules)


def collect_remote_dependency_sources(
    sources: set[str],
    namespace: Path,
    apm_modules: Path,
) -> None:
    """Collect fallback sources from explicit remote install layouts."""
    if add_dependency_source(sources, namespace, apm_modules):
        return

    for repo_or_project in child_dependency_dirs(namespace):
        if add_dependency_source(sources, repo_or_project, apm_modules):
            continue
        collect_known_subdirectory_sources(sources, repo_or_project, apm_modules)
        for ado_repo in child_dependency_dirs(repo_or_project):
            if add_dependency_source(sources, ado_repo, apm_modules):
                continue
            collect_known_subdirectory_sources(sources, ado_repo, apm_modules)


def collect_local_dependency_sources(
    sources: set[str],
    local_namespace: Path,
    apm_modules: Path,
) -> None:
    """Collect direct and parent-anchored local package roots."""
    for local_package in child_dependency_dirs(local_namespace):
        if add_dependency_source(sources, local_package, apm_modules):
            continue
        for anchored_package in child_dependency_dirs(local_package):
            add_dependency_source(sources, anchored_package, apm_modules)


def bounded_dependency_hook_sources(apm_modules: Path) -> set[str]:
    """Fallback source scan limited to known apm_modules package layouts."""
    sources: set[str] = set()
    for package_root in child_dependency_dirs(apm_modules):
        if package_root.name == "_local":
            collect_local_dependency_sources(sources, package_root, apm_modules)
            continue
        collect_remote_dependency_sources(sources, package_root, apm_modules)
    return sources


def dependency_hook_sources(project_root: Path) -> set[str]:
    """Return markers corresponding to installed dependency roots."""
    apm_modules = project_root / "apm_modules"
    if not apm_modules.is_dir():
        return set()

    identities, lockfile_readable = lockfile_dependency_identities(project_root)
    if lockfile_readable:
        sources: set[str] = set()
        for relative_path, canonical_identity in identities:
            package_path = safe_dependency_path(apm_modules, relative_path)
            if package_path is None or not add_dependency_source(
                sources,
                package_path,
                apm_modules,
            ):
                continue
            sources.add(canonical_identity)
        return sources
    return bounded_dependency_hook_sources(apm_modules)


def legacy_source_marker_is_unambiguous(
    package_info,
    project_root: Path,
    legacy_marker: str,
) -> bool:
    """Return True when an old leaf marker names only this dependency."""
    try:
        dependency_ref = vars(package_info).get("dependency_ref")
    except TypeError:
        dependency_ref = None
    if dependency_ref is None:
        return False
    identities, lockfile_readable = lockfile_dependency_identities(project_root)
    if not lockfile_readable:
        return False
    current = dependency_hook_source_marker(dependency_ref)
    matching = {
        canonical_identity
        for relative_path, canonical_identity in identities
        if Path(relative_path).name == legacy_marker
    }
    return matching == {current}
