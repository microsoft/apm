"""Registry-derived deployed primitive boundaries, independent of ownership."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from stat import S_ISLNK

from apm_cli.compilation.root_context_protection import root_context_filename
from apm_cli.integration.hook_integrator import native_hook_config
from apm_cli.integration.targets import PrimitiveMapping, TargetProfile
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
)


@dataclass(frozen=True)
class PrimitiveSurface:
    """One bounded native filename or pattern supplied by a target profile."""

    root: Path
    path: Path
    pattern: str | None
    target: str
    kind: str
    format_id: str
    prompt_fields: tuple[str, ...] = ()
    shared: bool = False
    external: bool = False
    user_scope: bool = False

    def contains(self, path: Path) -> bool:
        """Match a path without walking or resolving user-controlled links."""
        if self.pattern is None:
            return path == self.path
        return path.is_relative_to(self.path) and fnmatch.fnmatchcase(path.name, self.pattern)


def _mapping_surface(
    project_root: Path, target: TargetProfile, kind: str, mapping: PrimitiveMapping
) -> PrimitiveSurface | None:
    """Translate the existing deployment mapping, not an audit-local path list."""
    if kind == "canvas":
        return None
    root = target.managed_deploy_root or project_root
    base = (
        project_root / mapping.deploy_root
        if mapping.deploy_root
        else target.deploy_path(project_root)
    )
    if mapping.deploy_root:
        root = project_root
    path = target.skills_deploy_path(project_root) if kind == "skills" else base / mapping.subdir
    if not mapping.subdir:
        if mapping.extension.startswith("."):
            # Root-level aggregate mappings declare exact generated filenames.
            return None
        path /= mapping.extension.lstrip("/")
        pattern = None
    elif mapping.extension.startswith("/"):
        pattern = mapping.extension.lstrip("/")
    else:
        pattern = f"*{mapping.extension}"
    return PrimitiveSurface(
        root,
        path,
        pattern,
        target.name,
        kind,
        mapping.format_id,
        mapping.prompt_fields,
        external=root != project_root,
    )


def primitive_surfaces(
    project_root: Path,
    targets: Sequence[TargetProfile],
    *,
    user_scope: bool = False,
) -> tuple[PrimitiveSurface, ...]:
    """Describe recognized native files using scope-resolved target/hook owners."""
    surfaces: list[PrimitiveSurface] = []
    for target in targets:
        for kind, mapping in target.primitives.items():
            surface = _mapping_surface(project_root, target, kind, mapping)
            if surface is not None:
                surfaces.append(surface)
        root = target.managed_deploy_root or project_root
        for filename in target.generated_files:
            surfaces.append(
                PrimitiveSurface(
                    root,
                    target.deploy_path(project_root, filename),
                    None,
                    target.name,
                    "context",
                    "markdown",
                    external=root != project_root,
                )
            )
        context = root_context_filename(target.compile_family)
        if context is not None:
            context_path = (
                target.deploy_path(project_root, context) if user_scope else project_root / context
            )
            surfaces.append(
                PrimitiveSurface(
                    root if user_scope else project_root,
                    context_path,
                    None,
                    target.name,
                    "context",
                    "markdown",
                    external=user_scope and root != project_root,
                )
            )
        config = native_hook_config(target.name)
        mapping = target.primitives.get("hooks")
        if config is not None and mapping is not None:
            surfaces.append(
                PrimitiveSurface(
                    root,
                    target.deploy_path(project_root, config.config_filename),
                    None,
                    target.name,
                    "hooks",
                    mapping.format_id,
                    shared=True,
                    external=root != project_root,
                )
            )
    # Exact shared configs override an identical primitive filename (hooks.json).
    return tuple(
        replace(surface, user_scope=user_scope)
        for surface in {(s.path, s.pattern): s for s in surfaces}.values()
    )


def safe_surface_path(surface: PrimitiveSurface, path: Path) -> bool:
    """Apply containment and reject symlinks at every component before reading."""
    try:
        root_linked = S_ISLNK(surface.root.lstat().st_mode)
    except FileNotFoundError:
        return False
    if root_linked or has_symlink_component(surface.root, path, raise_on_error=True):
        return False
    try:
        ensure_path_within(path, surface.root)
    except (PathTraversalError, RuntimeError):
        return False
    return True


def iter_surface_files(surface: PrimitiveSurface) -> Iterator[Path]:
    """Enumerate only a registered primitive directory, never a tool root."""
    if not safe_surface_path(surface, surface.path):
        return
    if surface.pattern is None:
        if surface.path.exists():
            yield surface.path
        return
    if not surface.path.exists():
        return
    if not surface.path.is_dir():
        yield surface.path
        return

    def fail(exc: OSError) -> None:
        raise exc

    for directory, dirs, files in os.walk(surface.path, followlinks=False, onerror=fail):
        current = Path(directory)
        dirs[:] = [
            name
            for name in dirs
            if not name.startswith(".")
            and name not in {"__pycache__", "node_modules"}
            and not (current / name).is_symlink()
        ]
        for filename in sorted(files):
            path = current / filename
            if surface.contains(path) and safe_surface_path(surface, path):
                yield path
