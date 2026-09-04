"""Authorized source paths for one package deployment.

The plan is built only after target, subset, and executable authorization.
It is the single source of truth for both the pre-deploy security scan and
skill materialization, so a source-only fixture cannot become deployable
because it happens to be scanned.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.install.cache_pin import MARKER_FILENAME
from apm_cli.models.dependency.subsets import skill_subset_filter_tokens
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within_resolved,
    has_symlink_component,
)
from apm_cli.utils.paths import portable_relpath


def _is_safe_source_path(path: Path, source_root: Path) -> bool:
    """Return whether a source candidate stays in the real package tree."""
    try:
        path.relative_to(source_root)
    except ValueError:
        return False
    if has_symlink_component(source_root, path):
        return False
    try:
        ensure_path_within_resolved(path, source_root)
    except (OSError, PathTraversalError, RuntimeError):
        return False
    return True


@dataclass(frozen=True)
class DeployableSourcePlan:
    """Concrete, authorized source files for one package deployment."""

    source_root: Path
    paths: frozenset[str]
    selected_skill_names: frozenset[str] | None = None
    authorized_parent_prefixes: frozenset[str] = frozenset()
    hook_source_selection: Any | None = None
    plugin_bin_deployable: bool = False

    def __post_init__(self) -> None:
        """Index authorized parents for constant-time copy filtering."""
        if self.authorized_parent_prefixes:
            return
        prefixes = {
            parent.as_posix()
            for path in self.paths
            for parent in Path(path).parents
            if parent.as_posix() != "."
        }
        object.__setattr__(self, "authorized_parent_prefixes", frozenset(prefixes))

    @classmethod
    def create(
        cls,
        package_info: Any,
        targets: list[Any],
        *,
        skill_subset: tuple[str, ...] | None,
        hooks_approved: bool,
        canvas_approved: bool,
        skip_bin: bool,
        plugin_bin_deployable: bool = False,
    ) -> DeployableSourcePlan:
        """Build the authorized deploy set after all deployment gates resolve."""
        source_root = Path(package_info.install_path).resolve()
        paths: set[str] = set()
        selected_skill_names: frozenset[str] | None = None
        hook_source_selection = None
        target_primitives = {primitive for target in targets for primitive in target.primitives}

        def add_file(path: Path) -> None:
            if _is_safe_source_path(path, source_root) and path.is_file():
                paths.add(portable_relpath(path, source_root))

        def tree_files(root: Path) -> Iterator[Path]:
            if not _is_safe_source_path(root, source_root) or not root.is_dir():
                return
            for parent, directory_names, file_names in os.walk(root, followlinks=False):
                parent_path = Path(parent)
                directory_names[:] = [
                    name
                    for name in directory_names
                    if _is_safe_source_path(parent_path / name, source_root)
                ]
                yield from (parent_path / name for name in file_names)

        def add_tree(root: Path) -> None:
            for path in tree_files(root):
                add_file(path)

        def add_matching_files(root: Path, pattern: str) -> None:
            for path in tree_files(root):
                if path.match(pattern):
                    add_file(path)

        def add_direct_matching_files(root: Path, pattern: str) -> None:
            if not _is_safe_source_path(root, source_root) or not root.is_dir():
                return
            for path in root.iterdir():
                if path.match(pattern):
                    add_file(path)

        if "prompts" in target_primitives or "commands" in target_primitives:
            add_direct_matching_files(source_root, "*.prompt.md")
            add_matching_files(source_root / ".apm" / "prompts", "*.prompt.md")

        if "agents" in target_primitives:
            add_direct_matching_files(source_root, "*.agent.md")
            add_matching_files(source_root / ".apm" / "agents", "*.md")

        if "instructions" in target_primitives:
            add_matching_files(source_root / ".apm" / "instructions", "*.instructions.md")

        if hooks_approved and "hooks" in target_primitives:
            from apm_cli.integration.hook_integrator import HookIntegrator

            hook_target_names = [
                target.name
                for target in targets
                if "hooks" in target.primitives and hasattr(target, "name")
            ]
            hook_source_selection = HookIntegrator.select_deployable_hook_sources(
                source_root,
                hook_target_names,
            )
            for path in hook_source_selection.files:
                add_file(path)

        if canvas_approved and "canvas" in target_primitives:
            from apm_cli.integration.canvas_integrator import CanvasIntegrator

            for bundle in CanvasIntegrator.find_canvas_bundles(source_root):
                add_tree(bundle)

        if "skills" in target_primitives:
            from apm_cli.models.apm_package import PackageType

            is_marketplace_plugin = (
                getattr(package_info, "package_type", None) is PackageType.MARKETPLACE_PLUGIN
            )
            source_skill = source_root / "SKILL.md"
            has_root_skill = (
                _is_safe_source_path(source_skill, source_root) and source_skill.is_file()
            )
            if has_root_skill:
                for path in tree_files(source_root):
                    relative = path.relative_to(source_root)
                    if (
                        relative.parts[0] == ".apm"
                        or path.name == MARKER_FILENAME
                        or (skip_bin and relative.parts[0] == "bin")
                        or (
                            is_marketplace_plugin
                            and len(relative.parts) == 1
                            and path.name == "apm.yml"
                        )
                    ):
                        continue
                    add_file(path)

            selected = skill_subset_filter_tokens(skill_subset)
            selected_skill_names = frozenset(selected) if selected is not None else None
            if is_marketplace_plugin and not has_root_skill:
                from apm_cli.deps.plugin_parser import normalized_plugin_skill_sources

                plugin_sources, _declared = normalized_plugin_skill_sources(source_root)
                candidates = [
                    plugin_sources[name]
                    for name in (
                        sorted(selected) if selected is not None else sorted(plugin_sources)
                    )
                    if name in plugin_sources
                ]
            else:
                candidates = []
                for skills_root in (
                    source_root / "skills",
                    source_root / ".apm" / "skills",
                ):
                    if (
                        not _is_safe_source_path(skills_root, source_root)
                        or not skills_root.is_dir()
                        or (
                            has_root_skill
                            and selected is None
                            and skills_root == source_root / "skills"
                        )
                    ):
                        continue
                    candidates.extend(
                        [skills_root / name for name in sorted(selected)]
                        if selected is not None
                        else skills_root.iterdir()
                    )

            for skill_dir in candidates:
                if not (
                    _is_safe_source_path(skill_dir, source_root)
                    and skill_dir.is_dir()
                    and _is_safe_source_path(skill_dir / "SKILL.md", source_root)
                    and (skill_dir / "SKILL.md").is_file()
                    and (selected is None or skill_dir.name in selected)
                ):
                    continue
                for path in tree_files(skill_dir):
                    relative = path.relative_to(skill_dir)
                    if skip_bin and relative.parts[0] == "bin":
                        continue
                    add_file(path)

        if plugin_bin_deployable:
            add_tree(source_root / "bin")
            add_file(source_root / ".claude-plugin" / "plugin.json")

        return cls(
            source_root=source_root,
            paths=frozenset(paths),
            selected_skill_names=selected_skill_names,
            hook_source_selection=hook_source_selection,
            plugin_bin_deployable=plugin_bin_deployable,
        )

    def includes(self, relative_path: str) -> bool:
        """Return whether a portable source-relative path is authorized."""
        return relative_path.replace("\\", "/") in self.paths

    def scan_security(self, *, policy, force: bool = False):
        """Scan exactly the source files authorized by this plan."""
        from apm_cli.security.gate import SecurityGate

        return SecurityGate.scan_files(
            self.source_root,
            policy=policy,
            force=force,
            paths=self.paths,
        )

    def copy_ignore(self, directory: str, contents: list[str]) -> list[str]:
        """Return source entries excluded from a skill copy by this plan."""
        current = Path(directory)
        ignored: list[str] = []
        for name in contents:
            candidate = current / name
            if not _is_safe_source_path(candidate, self.source_root):
                ignored.append(name)
                continue
            relative = portable_relpath(candidate, self.source_root)
            if self.includes(relative) or relative in self.authorized_parent_prefixes:
                continue
            ignored.append(name)
        return ignored
