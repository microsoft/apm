"""Package-level skill integration orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class PackageSkillContext:
    """Options and callbacks for package skill integration."""

    diagnostics: Any = None
    managed_files: set[str] | None = None
    force: bool = False
    logger: Any = None
    targets: Any = None
    skill_subset: Any = None
    scope: Any = None
    policy: Any = None
    skip_bin: bool = False
    should_install_fn: Callable[[Any], bool] | None = None
    result_cls: Any = None


def _skipped_result(
    result_cls: Any, sub_skills_count: int = 0, target_paths: list[Path] | None = None
) -> Any:
    """Build the standard skipped skill integration result."""
    return result_cls(
        skill_created=False,
        skill_updated=False,
        skill_skipped=True,
        skill_path=None,
        references_copied=0,
        links_resolved=0,
        sub_skills_promoted=sub_skills_count,
        target_paths=target_paths or [],
    )


def integrate_package_skill(
    link_rewriter: Any,
    package_info: Any,
    project_root: Path,
    context: PackageSkillContext,
) -> Any:
    """Integrate a package's skill into all active target directories."""
    if context.should_install_fn is None or context.result_cls is None:
        raise ValueError("PackageSkillContext requires should_install_fn and result_cls")

    if not context.should_install_fn(package_info):
        sub_count, sub_deployed = link_rewriter._promote_sub_skills_standalone(
            package_info,
            project_root,
            diagnostics=context.diagnostics,
            managed_files=context.managed_files,
            force=context.force,
            logger=context.logger,
            targets=context.targets,
            skill_subset=context.skill_subset,
            skip_bin=context.skip_bin,
        )
        return _skipped_result(context.result_cls, sub_count, sub_deployed)

    if package_info.dependency_ref and package_info.dependency_ref.is_virtual:
        if not package_info.dependency_ref.is_virtual_subdirectory():
            return _skipped_result(context.result_cls)

    package_path = package_info.install_path
    bin_paths: list[Path] = []
    bin_skip_reason: str | None = None
    from apm_cli.models.apm_package import PackageType as _PackageType

    if package_info.package_type == _PackageType.MARKETPLACE_PLUGIN:
        if context.skip_bin:
            bin_skip_reason = "not_approved"
        else:
            bin_paths, bin_skip_reason = link_rewriter._deploy_plugin_bin(
                package_info,
                project_root,
                context.targets,
                scope=context.scope,
                policy=context.policy,
                force=context.force,
                logger=context.logger,
            )

    source_skill_md = package_path / "SKILL.md"
    if source_skill_md.exists():
        if context.skill_subset:
            from apm_cli.utils.console import _rich_warning

            _rich_warning(
                f"--skill filter ignored for '{package_info.install_path.name}': "
                "package is a single CLAUDE_SKILL, not a SKILL_BUNDLE."
            )
        result = link_rewriter._integrate_native_skill(
            package_info,
            project_root,
            source_skill_md,
            diagnostics=context.diagnostics,
            managed_files=context.managed_files,
            force=context.force,
            logger=context.logger,
            targets=context.targets,
            skip_bin=context.skip_bin,
        )
        return link_rewriter._merge_bin_paths(result, bin_paths, bin_skip_reason)

    root_skills_dir = package_path / "skills"
    if root_skills_dir.is_dir() and any(
        (directory / "SKILL.md").exists()
        for directory in root_skills_dir.iterdir()
        if directory.is_dir()
    ):
        result = link_rewriter._integrate_skill_bundle(
            package_info,
            project_root,
            root_skills_dir,
            diagnostics=context.diagnostics,
            managed_files=context.managed_files,
            force=context.force,
            logger=context.logger,
            targets=context.targets,
            skill_subset=context.skill_subset,
            skip_bin=context.skip_bin,
        )
        return link_rewriter._merge_bin_paths(result, bin_paths, bin_skip_reason)

    sub_count, sub_deployed = link_rewriter._promote_sub_skills_standalone(
        package_info,
        project_root,
        diagnostics=context.diagnostics,
        managed_files=context.managed_files,
        force=context.force,
        logger=context.logger,
        targets=context.targets,
        skill_subset=context.skill_subset,
        skip_bin=context.skip_bin,
    )
    result = _skipped_result(context.result_cls, sub_count, sub_deployed)
    return link_rewriter._merge_bin_paths(result, bin_paths, bin_skip_reason)
