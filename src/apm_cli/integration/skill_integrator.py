"""Skill integration functionality for APM packages."""

import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from apm_cli.integration.base_integrator import BaseIntegrator

from . import skill_deploy as _skill_deploy
from .skill_naming import _skill_name_char_error
from .skill_naming import normalize_skill_name as normalize_skill_name
from .skill_naming import should_compile_instructions as should_compile_instructions
from .skill_naming import to_hyphen_case as to_hyphen_case


@dataclass
class SkillIntegrationResult:
    """Result of skill integration operation."""

    skill_created: bool
    skill_updated: bool
    skill_skipped: bool
    skill_path: Path | None
    references_copied: int
    links_resolved: int = 0
    sub_skills_promoted: int = 0
    bin_deployed: int = 0
    bin_skipped_reason: str | None = None
    target_paths: list[Path] | None = None

    def __post_init__(self) -> None:
        if self.target_paths is None:
            self.target_paths = []


def validate_skill_name(name: str) -> tuple[bool, str]:
    """Validate skill name per agentskills.io spec."""
    if not name:
        return (False, "Skill name cannot be empty")
    if len(name) > 64:
        return (False, f"Skill name must be 1-64 characters (got {len(name)})")
    if "--" in name:
        return (False, "Skill name cannot contain consecutive hyphens (--)")
    if name.startswith("-"):
        return (False, "Skill name cannot start with a hyphen")
    if name.endswith("-"):
        return (False, "Skill name cannot end with a hyphen")
    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", name):
        return (False, _skill_name_char_error(name))
    return (True, "")


def get_effective_type(package_info: Any) -> "PackageContentType":
    """Get effective package content type based on package structure."""
    from apm_cli.models.apm_package import PackageContentType, PackageType

    if package_info.package_type in (
        PackageType.CLAUDE_SKILL,
        PackageType.HYBRID,
        PackageType.SKILL_BUNDLE,
        PackageType.MARKETPLACE_PLUGIN,
    ):
        return PackageContentType.SKILL
    return PackageContentType.INSTRUCTIONS


def should_install_skill(package_info: Any) -> bool:
    """Determine if package should be installed as a native skill."""
    from apm_cli.models.apm_package import PackageContentType

    effective_type = get_effective_type(package_info)
    return effective_type in (PackageContentType.SKILL, PackageContentType.HYBRID)


def copy_skill_to_target(
    package_info: Any,
    source_path: Path,
    target_base: Path,
    targets: Any = None,
) -> list[Path]:
    """Copy a skill directory to all active target skills directories."""
    context = _skill_deploy.CopySkillContext(
        should_install_fn=should_install_skill,
        validate_name_fn=validate_skill_name,
        normalize_name_fn=normalize_skill_name,
        rewriter_factory=SkillIntegrator,
    )
    return _skill_deploy._copy_skill_to_target(
        package_info, source_path, target_base, targets, context
    )


class SkillIntegrator(BaseIntegrator):
    """Handles integration of native skill files for supported targets."""

    def __init__(self) -> None:
        self._native_skill_session_owners: dict[str, str] = {}

    def find_instruction_files(self, package_path: Path) -> list[Path]:
        """Find all instruction files in a package."""
        return _skill_deploy.find_instruction_files(package_path)

    def find_agent_files(self, package_path: Path) -> list[Path]:
        """Find all agent files in a package."""
        return _skill_deploy.find_agent_files(package_path)

    def find_prompt_files(self, package_path: Path) -> list[Path]:
        """Find all prompt files in a package."""
        return _skill_deploy.find_prompt_files(package_path)

    def find_context_files(self, package_path: Path) -> list[Path]:
        """Find all context and memory files in a package."""
        return _skill_deploy.find_context_files(package_path)

    @staticmethod
    def is_skill_dir_identical_to_source(dir_a: Path, dir_b: Path) -> bool:
        """Check if two directory trees have identical file contents."""
        return _skill_deploy.is_skill_dir_identical_to_source(dir_a, dir_b)

    @staticmethod
    def _dircmp_equal(dcmp: Any) -> bool:
        """Recursively check if dircmp shows identical contents."""
        return _skill_deploy._dircmp_equal(dcmp)

    def _resolve_markdown_links_in_skill_bundle(self, source_root: Path, target_root: Path) -> int:
        """Read copied skill markdown from source and write resolved target content."""
        return _skill_deploy._resolve_markdown_links_in_skill_bundle(self, source_root, target_root)

    @staticmethod
    def _copy_source_skill_tree(source_path: Path, skill_dir: Path) -> None:
        """Copy a standalone skill tree while excluding non-content files."""
        from apm_cli.security.gate import ignore_non_content

        shutil.copytree(source_path, skill_dir, ignore=ignore_non_content)

    @staticmethod
    def _copy_native_skill_tree(package_path: Path, target_skill_dir: Path) -> None:
        """Copy a native skill tree while excluding non-content files and .apm."""
        from apm_cli.security.gate import ignore_non_content

        def ignore_non_content_and_apm(directory: str, contents: list[str]) -> list[str]:
            ignored = set(ignore_non_content(directory, contents))
            if ".apm" in contents:
                ignored.add(".apm")
            return list(ignored)

        shutil.copytree(package_path, target_skill_dir, ignore=ignore_non_content_and_apm)

    @staticmethod
    def _copy_promoted_skill_tree(sub_skill_path: Path, target: Path) -> None:
        """Copy a promoted sub-skill tree while excluding non-content files."""
        from apm_cli.security.gate import ignore_non_content

        shutil.copytree(sub_skill_path, target, dirs_exist_ok=True, ignore=ignore_non_content)

    @staticmethod
    def _skill_subset_name_filter(skill_subset: tuple[str, ...] | None) -> set[str] | None:
        """Return promotion filter tokens for --skill subset values."""
        return _skill_deploy._skill_subset_name_filter(skill_subset)

    @staticmethod
    def _promote_sub_skills(
        sub_skills_dir: Path,
        target_skills_root: Path,
        parent_name: str,
        *,
        warn: bool = True,
        owned_by: dict[str, str] | None = None,
        diagnostics: Any = None,
        managed_files: set[str] | None = None,
        force: bool = False,
        project_root: Path | None = None,
        logger: Any = None,
        name_filter: set[str] | None = None,
        link_rewriter: Any = None,
    ) -> tuple[int, list[Path]]:
        """Promote sub-skills from a package skill directory."""
        return _skill_deploy._promote_sub_skills(
            sub_skills_dir,
            target_skills_root,
            parent_name,
            warn=warn,
            owned_by=owned_by,
            diagnostics=diagnostics,
            managed_files=managed_files,
            force=force,
            project_root=project_root,
            logger=logger,
            name_filter=name_filter,
            link_rewriter=link_rewriter,
        )

    @staticmethod
    def _build_ownership_maps(project_root: Path) -> tuple[dict[str, str], dict[str, str]]:
        """Read the lockfile once and build sub-skill and native-skill owner maps."""
        return _skill_deploy._build_ownership_maps(project_root)

    @staticmethod
    def _build_skill_ownership_map(project_root: Path) -> dict[str, str]:
        """Build a map of skill name to owner package name from the lockfile."""
        owned_by, _ = SkillIntegrator._build_ownership_maps(project_root)
        return owned_by

    @staticmethod
    def _build_native_skill_owner_map(project_root: Path) -> dict[str, str]:
        """Build a map of skill name to dependency key from the lockfile."""
        _, native_owners = SkillIntegrator._build_ownership_maps(project_root)
        return native_owners

    def _promote_sub_skills_standalone(
        self,
        package_info: Any,
        project_root: Path,
        diagnostics: Any = None,
        managed_files: set[str] | None = None,
        force: bool = False,
        logger: Any = None,
        targets: Any = None,
        skill_subset: Any = None,
    ) -> tuple[int, list[Path]]:
        """Promote sub-skills from a package that is not itself a skill."""
        return _skill_deploy._promote_sub_skills_standalone(
            self,
            package_info,
            project_root,
            diagnostics=diagnostics,
            managed_files=managed_files,
            force=force,
            logger=logger,
            targets=targets,
            skill_subset=skill_subset,
        )

    def _integrate_native_skill(
        self,
        package_info: Any,
        project_root: Path,
        source_skill_md: Path,
        diagnostics: Any = None,
        managed_files: set[str] | None = None,
        force: bool = False,
        logger: Any = None,
        targets: Any = None,
    ) -> SkillIntegrationResult:
        """Copy a native skill to all active targets."""
        fields = _skill_deploy._integrate_native_skill(
            self,
            package_info,
            project_root,
            source_skill_md,
            diagnostics=diagnostics,
            managed_files=managed_files,
            force=force,
            logger=logger,
            targets=targets,
        )
        return SkillIntegrationResult(
            skill_created=fields["skill_created"],
            skill_updated=fields["skill_updated"],
            skill_skipped=False,
            skill_path=fields["primary_skill_md"],
            references_copied=fields["files_copied"],
            links_resolved=0,
            sub_skills_promoted=fields["sub_skills_promoted"],
            target_paths=fields["target_paths"],
        )

    def _integrate_skill_bundle(
        self,
        package_info: Any,
        project_root: Path,
        skills_dir: Path,
        diagnostics: Any = None,
        managed_files: set[str] | None = None,
        force: bool = False,
        logger: Any = None,
        targets: Any = None,
        skill_subset: Any = None,
    ) -> SkillIntegrationResult:
        """Promote every skill in a skill bundle's top-level skills directory."""
        fields = _skill_deploy._integrate_skill_bundle(
            self,
            package_info,
            project_root,
            skills_dir,
            diagnostics=diagnostics,
            managed_files=managed_files,
            force=force,
            logger=logger,
            targets=targets,
            skill_subset=skill_subset,
        )
        return SkillIntegrationResult(**fields)

    def integrate_package_skill(
        self,
        package_info: Any,
        project_root: Path,
        diagnostics: Any = None,
        managed_files: set[str] | None = None,
        force: bool = False,
        logger: Any = None,
        targets: Any = None,
        skill_subset: Any = None,
        scope: Any = None,
        policy: Any = None,
    ) -> SkillIntegrationResult:
        """Integrate a package's skill into all active target directories."""
        context = _skill_deploy.PackageSkillContext(
            diagnostics=diagnostics,
            managed_files=managed_files,
            force=force,
            logger=logger,
            targets=targets,
            skill_subset=skill_subset,
            scope=scope,
            policy=policy,
            should_install_fn=should_install_skill,
            result_cls=SkillIntegrationResult,
        )
        return _skill_deploy.integrate_package_skill(self, package_info, project_root, context)

    @staticmethod
    def _merge_bin_paths(
        result: SkillIntegrationResult,
        bin_paths: list[Path],
        skip_reason: str | None = None,
    ) -> SkillIntegrationResult:
        """Fold deployed plugin bin and manifest paths into a skill result."""
        if not bin_paths and skip_reason is None:
            return result
        updates: dict[str, Any] = {}
        if bin_paths:
            updates["bin_deployed"] = len(bin_paths)
            updates["skill_skipped"] = False
            updates["target_paths"] = (result.target_paths or []) + bin_paths
        if skip_reason is not None:
            updates["bin_skipped_reason"] = skip_reason
        return replace(result, **updates)

    def _deploy_plugin_bin(
        self,
        package_info: Any,
        project_root: Path,
        targets: Any,
        scope: Any = None,
        policy: Any = None,
        force: bool = False,
        logger: Any = None,
    ) -> tuple[list[Path], str | None]:
        """Deploy bin executables and plugin manifest for a marketplace plugin."""
        return _skill_deploy._deploy_plugin_bin(
            self,
            package_info,
            project_root,
            targets,
            scope=scope,
            policy=policy,
            force=force,
            logger=logger,
        )

    @staticmethod
    def _bin_deploy_denied(package_info: Any, policy: Any, logger: Any) -> bool:
        """Return True when policy opts the package out of bin deployment."""
        return _skill_deploy._bin_deploy_denied(package_info, policy, logger)

    def _deploy_bin_files(
        self,
        bin_dir: Path,
        skill_base: Path,
        rel_prefix: str,
        force: bool,
        logger: Any,
    ) -> list[Path]:
        """Copy bin executables into a deployed skill directory."""
        return _skill_deploy._deploy_bin_files(bin_dir, skill_base, rel_prefix, force, logger)

    def _deploy_plugin_manifest(
        self,
        package_path: Path,
        skill_base: Path,
        rel_prefix: str,
        force: bool,
        logger: Any,
    ) -> Path | None:
        """Copy .claude-plugin/plugin.json next to the deployed bin directory."""
        return _skill_deploy._deploy_plugin_manifest(
            package_path, skill_base, rel_prefix, force, logger
        )

    @staticmethod
    def _copy_plugin_file(
        src_file: Path,
        dest_file: Path,
        *,
        force: bool,
        make_executable: bool,
        logger: Any,
        rel_label: str,
    ) -> None:
        """Hash-gated copy of one plugin file, optionally marking it executable."""
        _skill_deploy._copy_plugin_file(
            src_file,
            dest_file,
            force=force,
            make_executable=make_executable,
            logger=logger,
            rel_label=rel_label,
        )

    def sync_integration(
        self,
        apm_package: Any,
        project_root: Path,
        managed_files: set[str] | None = None,
        targets: Any = None,
    ) -> dict[str, int]:
        """Sync skill directories with currently installed packages."""
        return _skill_deploy.sync_integration(
            self, apm_package, project_root, managed_files, targets
        )

    def _clean_orphaned_skills(
        self,
        skills_dir: Path,
        installed_skill_names: set[str],
        *,
        project_root: Path | None = None,
    ) -> dict[str, int]:
        """Clean orphaned skills from a skills directory."""
        return _skill_deploy._clean_orphaned_skills(
            skills_dir,
            installed_skill_names,
            project_root=project_root,
            get_lockfile_owned_fn=self._get_lockfile_owned_agent_skills,
        )

    @staticmethod
    def _get_lockfile_owned_agent_skills(project_root: Path) -> set[str]:
        """Return skill names under .agents/skills in the lockfile."""
        return _skill_deploy._get_lockfile_owned_agent_skills(project_root)
