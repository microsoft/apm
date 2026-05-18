"""Skill integration functionality for APM packages (Claude Code & Cursor support)."""

import filecmp
from dataclasses import dataclass
from pathlib import Path

from apm_cli.integration.base_integrator import BaseIntegrator

from .opts import SkillOpts, SkillPromoteOpts


# DEPRECATED -- use IntegrationResult directly for new code.
# Kept for backward compatibility. The fields map as follows:
# skill_created -> IntegrationResult.skill_created
# sub_skills_promoted -> IntegrationResult.sub_skills_promoted
# skill_path, references_copied -> not mapped (skill-internal)
@dataclass
class SkillIntegrationResult:
    """Result of skill integration operation."""

    skill_created: bool
    skill_updated: bool
    skill_skipped: bool
    skill_path: Path | None
    references_copied: int  # Now tracks total files copied to subdirectories
    links_resolved: int = 0  # Kept for backwards compatibility
    sub_skills_promoted: int = 0  # Number of sub-skills promoted to top-level
    target_paths: list[Path] = None  # All deployed directories (for deployed_files manifest)

    def __post_init__(self):
        if self.target_paths is None:
            self.target_paths = []


class SkillIntegrator(BaseIntegrator):
    """Handles integration of native SKILL.md files for Claude Code, Cursor, and VS Code.

    Claude Skills Spec:
    - SKILL.md files provide structured context for Claude Code
    - YAML frontmatter with name, description, and metadata
    - Markdown body with instructions and agent definitions
    - references/ subdirectory for prompt files
    """

    def __init__(self) -> None:
        # In-memory map of skill_name -> dep.get_unique_key() updated as each native
        # skill is deployed in the current install run.  Complements the lockfile-based
        # map so that same-manifest collisions are detected before the lockfile is written.
        self._native_skill_session_owners: dict[str, str] = {}

    def find_instruction_files(self, package_path: Path) -> list[Path]:
        """Find all instruction files in a package.

        Searches in:
        - .apm/instructions/ subdirectory

        Args:
            package_path: Path to the package directory

        Returns:
            List[Path]: List of absolute paths to instruction files
        """
        instruction_files = []

        # Search in .apm/instructions/
        apm_instructions = package_path / ".apm" / "instructions"
        if apm_instructions.exists():
            instruction_files.extend(apm_instructions.glob("*.instructions.md"))

        return instruction_files

    def find_agent_files(self, package_path: Path) -> list[Path]:
        """Find all agent files in a package.

        Searches in:
        - .apm/agents/ subdirectory

        Args:
            package_path: Path to the package directory

        Returns:
            List[Path]: List of absolute paths to agent files
        """
        agent_files = []

        # Search in .apm/agents/
        apm_agents = package_path / ".apm" / "agents"
        if apm_agents.exists():
            agent_files.extend(apm_agents.glob("*.agent.md"))

        return agent_files

    def find_prompt_files(self, package_path: Path) -> list[Path]:
        """Find all prompt files in a package.

        Searches in:
        - Package root directory
        - .apm/prompts/ subdirectory

        Args:
            package_path: Path to the package directory

        Returns:
            List[Path]: List of absolute paths to prompt files
        """
        prompt_files = []

        # Search in package root
        if package_path.exists():
            prompt_files.extend(package_path.glob("*.prompt.md"))

        # Search in .apm/prompts/
        apm_prompts = package_path / ".apm" / "prompts"
        if apm_prompts.exists():
            prompt_files.extend(apm_prompts.glob("*.prompt.md"))

        return prompt_files

    def find_context_files(self, package_path: Path) -> list[Path]:
        """Find all context/memory files in a package.

        Searches in:
        - .apm/context/ subdirectory
        - .apm/memory/ subdirectory

        Args:
            package_path: Path to the package directory

        Returns:
            List[Path]: List of absolute paths to context files
        """
        context_files = []

        # Search in .apm/context/
        apm_context = package_path / ".apm" / "context"
        if apm_context.exists():
            context_files.extend(apm_context.glob("*.context.md"))

        # Search in .apm/memory/
        apm_memory = package_path / ".apm" / "memory"
        if apm_memory.exists():
            context_files.extend(apm_memory.glob("*.memory.md"))

        return context_files

    @staticmethod
    def _dirs_equal(dir_a: Path, dir_b: Path) -> bool:
        """Check if two directory trees have identical file contents."""
        dcmp = filecmp.dircmp(str(dir_a), str(dir_b))
        return SkillIntegrator._dircmp_equal(dcmp)

    @staticmethod
    def _dircmp_equal(dcmp) -> bool:
        """Recursively check if dircmp shows identical contents."""
        if dcmp.left_only or dcmp.right_only or dcmp.funny_files:
            return False
        _, mismatches, errors = filecmp.cmpfiles(
            dcmp.left, dcmp.right, dcmp.common_files, shallow=False
        )
        if mismatches or errors:
            return False
        return all(SkillIntegrator._dircmp_equal(sub_dcmp) for sub_dcmp in dcmp.subdirs.values())

    @staticmethod
    @staticmethod
    def _promote_sub_skills(
        sub_skills_dir: Path,
        target_skills_root: Path,
        parent_name: str,
        opts: SkillPromoteOpts | None = None,
        **kwargs,
    ) -> tuple[int, list[Path]]:
        promote_opts = opts or SkillPromoteOpts(**kwargs)
        return _promotion._promote_sub_skills(
            sub_skills_dir,
            target_skills_root,
            parent_name,
            promote_opts,
        )

    @staticmethod
    def _build_ownership_maps(project_root: Path) -> tuple[dict[str, str], dict[str, str]]:
        """Read the lockfile once and build two ownership maps.

        Returns a tuple of:
        - owned_by: skill_name -> last-segment owner name, for sub-skill self-overwrite detection.
        - native_owners: skill_name -> dep.get_unique_key(), for native-skill cross-package
          collision detection.  Only paths under a ``/skills/`` prefix are included to avoid
          false attribution from non-skill deployed_files entries (prompts, hooks, commands, etc.).
        """
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        owned_by: dict[str, str] = {}
        native_owners: dict[str, str] = {}
        lockfile = LockFile.read(get_lockfile_path(project_root))
        if not lockfile:
            return owned_by, native_owners
        for dep in lockfile.get_package_dependencies():
            short_owner = (dep.virtual_path or dep.repo_url).rsplit("/", 1)[-1]
            unique_key = dep.get_unique_key()
            for deployed_path in dep.deployed_files:
                normalized = deployed_path.rstrip("/").replace("\\", "/")
                skill_name = normalized.rsplit("/", 1)[-1]
                # Both maps cover all paths for sub-skill self-overwrite tracking.
                owned_by[skill_name] = short_owner
                # Native-owner map is scoped to skill paths only to avoid false
                # attribution from prompts/hooks/commands that share a leaf name.
                if "/skills/" in normalized:
                    native_owners[skill_name] = unique_key
        return owned_by, native_owners

    @staticmethod
    def _build_skill_ownership_map(project_root: Path) -> dict[str, str]:
        """Build a map of skill_name -> owner_package_name from the lockfile.

        Used to distinguish self-overwrites (no warning) from cross-package
        conflicts (warning) when promoting sub-skills.
        """
        owned_by, _ = SkillIntegrator._build_ownership_maps(project_root)
        return owned_by

    @staticmethod
    def _build_native_skill_owner_map(project_root: Path) -> dict[str, str]:
        """Build a map of skill_name -> dep.get_unique_key() from the lockfile.

        Scoped to ``/skills/`` paths only -- see ``_build_ownership_maps`` for details.
        """
        _, native_owners = SkillIntegrator._build_ownership_maps(project_root)
        return native_owners

    def _promote_sub_skills_standalone(
        self,
        package_info,
        project_root: Path,
        opts: SkillOpts | None = None,
        *,
        targets: object = None,
    ) -> tuple[int, list[Path]]:
        if opts is None and targets is not None:
            opts = SkillOpts(targets=targets)
        return _promotion._promote_sub_skills_standalone(self, package_info, project_root, opts)

    def _integrate_native_skill(
        self,
        package_info,
        project_root: Path,
        source_skill_md: Path,
        opts: SkillOpts | None = None,
    ) -> SkillIntegrationResult:
        return _native._integrate_native_skill(
            self,
            package_info,
            project_root,
            source_skill_md,
            opts,
        )

    def _integrate_skill_bundle(
        self,
        package_info,
        project_root: Path,
        skills_dir: Path,
        opts: SkillOpts | None = None,
    ) -> SkillIntegrationResult:
        return _native._integrate_skill_bundle(
            self,
            package_info,
            project_root,
            skills_dir,
            opts,
        )

    def integrate_package_skill(
        self,
        package_info,
        project_root: Path,
        opts: SkillOpts | None = None,
        **kwargs,
    ) -> SkillIntegrationResult:
        skill_opts = opts or SkillOpts(**kwargs)
        return _native.integrate_package_skill(
            self,
            package_info,
            project_root,
            skill_opts,
        )

    def sync_integration(
        self, apm_package, project_root: Path, managed_files: set | None = None, targets=None
    ) -> dict[str, int]:
        return _sync.sync_integration(self, apm_package, project_root, managed_files, targets)

    def _clean_orphaned_skills(
        self, skills_dir: Path, installed_skill_names: set, *, project_root: Path | None = None
    ) -> dict[str, int]:
        return _sync._clean_orphaned_skills(
            self, skills_dir, installed_skill_names, project_root=project_root
        )

    @staticmethod
    @staticmethod
    def _get_lockfile_owned_agent_skills(project_root: Path) -> set[str]:
        return _sync._get_lockfile_owned_agent_skills(project_root)


from . import native as _native
from . import promotion as _promotion
from . import sync as _sync
