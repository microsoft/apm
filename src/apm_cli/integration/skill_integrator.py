"""Skill integration functionality for APM packages (Claude Code & Cursor support)."""

import filecmp
import hashlib  # noqa: F401
import shutil
from datetime import datetime  # noqa: F401
from pathlib import Path
from typing import Dict, List, Optional  # noqa: F401, UP035

import frontmatter  # noqa: F401

from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.skill_helpers import (
    SkillIntegrationResult,
    copy_skill_to_target,  # noqa: F401
    get_effective_type,  # noqa: F401
    normalize_skill_name,
    should_compile_instructions,  # noqa: F401
    should_install_skill,
    to_hyphen_case,  # noqa: F401
    validate_skill_name,
)


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
        for sub_dcmp in dcmp.subdirs.values():  # noqa: SIM110
            if not SkillIntegrator._dircmp_equal(sub_dcmp):
                return False
        return True

    @staticmethod
    def _promote_sub_skills(
        sub_skills_dir: Path,
        target_skills_root: Path,
        parent_name: str,
        *,
        warn: bool = True,
        owned_by: dict[str, str] | None = None,
        diagnostics=None,
        managed_files=None,
        force: bool = False,
        project_root: Path | None = None,
        logger=None,
        name_filter: "set | None" = None,
    ) -> tuple[int, list[Path]]:
        """Promote sub-skills from .apm/skills/ to top-level skill entries.

        Args:
            sub_skills_dir: Path to the .apm/skills/ directory in the source package.
            target_skills_root: Root skills directory (e.g. .github/skills/ or .claude/skills/).
            parent_name: Name of the parent skill (used in warning messages).
            warn: Whether to emit a warning on name collisions.
            owned_by: Map of skill_name -> owner_package_name from the lockfile.
                When provided, warnings are suppressed for self-overwrites.
            diagnostics: Optional DiagnosticCollector for deferred warning output.
            project_root: Project root for computing relative diagnostic paths.

        Returns:
            tuple[int, list[Path]]: (count of promoted sub-skills, list of deployed dir paths)
        """
        promoted = 0
        deployed = []
        if not sub_skills_dir.is_dir():
            return promoted, deployed

        # Compute project-relative prefix for consistent path reporting
        if project_root is not None:
            try:
                rel_prefix = target_skills_root.relative_to(project_root).as_posix()
            except ValueError:
                # Dynamic-root targets (cowork): use synthetic prefix
                # when the skills root lives outside the project tree.
                rel_prefix = target_skills_root.name
        else:
            rel_prefix = target_skills_root.name

        for sub_skill_path in sub_skills_dir.iterdir():
            if not sub_skill_path.is_dir():
                continue
            if not (sub_skill_path / "SKILL.md").exists():
                continue
            raw_sub_name = sub_skill_path.name
            # --skill filter: skip skills not in the requested subset
            if name_filter is not None and raw_sub_name not in name_filter:
                continue
            is_valid, _ = validate_skill_name(raw_sub_name)
            sub_name = raw_sub_name if is_valid else normalize_skill_name(raw_sub_name)
            target = target_skills_root / sub_name
            rel_path = f"{rel_prefix}/{sub_name}"
            if target.exists():
                # Content-identical Ã¢â€ â€™ skip entirely (no copy, no warning)
                if SkillIntegrator._dirs_equal(sub_skill_path, target):
                    promoted += 1
                    deployed.append(target)
                    continue

                # Check if this is a user-authored skill (not managed by APM)
                is_managed = (
                    managed_files is not None and rel_path.replace("\\", "/") in managed_files
                )
                prev_owner = (owned_by or {}).get(sub_name)
                is_self_overwrite = prev_owner is not None and prev_owner == parent_name

                if managed_files is not None and not is_managed and not is_self_overwrite:
                    # User-authored skill Ã¢â‚¬â€ respect force flag
                    if not force:
                        if diagnostics is not None:
                            diagnostics.skip(rel_path, package=parent_name)
                        elif logger:
                            logger.warning(
                                f"Skipping skill '{sub_name}' -- local skill exists (not managed by APM). "
                                f"Use 'apm install --force' to overwrite."
                            )
                        else:
                            try:
                                from apm_cli.utils.console import _rich_warning

                                _rich_warning(
                                    f"Skipping skill '{sub_name}' -- local skill exists (not managed by APM). "
                                    f"Use 'apm install --force' to overwrite."
                                )
                            except ImportError:
                                pass
                        continue  # SKIP Ã¢â‚¬â€ protect user content

                if warn and not is_self_overwrite:
                    if diagnostics is not None:
                        diagnostics.overwrite(
                            path=rel_path,
                            package=parent_name,
                            detail=f"Skill '{sub_name}' replaced -- previously from another package",
                        )
                    elif logger:
                        logger.warning(
                            f"Sub-skill '{sub_name}' from '{parent_name}' overwrites existing skill at {rel_path}"
                        )
                    else:
                        try:
                            from apm_cli.utils.console import _rich_warning

                            _rich_warning(
                                f"Sub-skill '{sub_name}' from '{parent_name}' overwrites existing skill at {rel_path}"
                            )
                        except ImportError:
                            pass
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
            from apm_cli.security.gate import ignore_non_content

            shutil.copytree(sub_skill_path, target, dirs_exist_ok=True, ignore=ignore_non_content)
            promoted += 1
            deployed.append(target)
        return promoted, deployed

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
        diagnostics=None,
        managed_files=None,
        force: bool = False,
        logger=None,
        targets=None,
    ) -> tuple[int, list[Path]]:
        """Promote sub-skills from a package that is NOT itself a skill.

        Packages typed as INSTRUCTIONS may still ship sub-skills under
        ``.apm/skills/``.  This method promotes them to all active targets
        that support skills, without creating a top-level skill entry for
        the parent package.

        Args:
            package_info: PackageInfo object with package metadata.
            project_root: Root directory of the project.
            targets: Optional explicit list of TargetProfile objects.

        Returns:
            tuple[int, list[Path]]: (count of promoted sub-skills, list of deployed dirs)
        """
        package_path = package_info.install_path
        sub_skills_dir = package_path / ".apm" / "skills"
        if not sub_skills_dir.is_dir():
            return 0, []

        if targets is None:
            from apm_cli.integration.targets import active_targets

            targets = active_targets(project_root)

        parent_name = package_path.name
        owned_by = self._build_skill_ownership_map(project_root)
        count = 0
        all_deployed: list[Path] = []
        seen_skill_dirs: set[Path] = set()

        for idx, target in enumerate(targets):
            if not target.supports("skills"):
                continue

            is_primary = idx == 0  # first active target owns diagnostics
            skills_mapping = target.primitives["skills"]
            # Dynamic-root targets (cowork): use resolved_deploy_root.
            if target.resolved_deploy_root is not None:
                target_skills_root = target.resolved_deploy_root
            else:
                effective_root = skills_mapping.deploy_root or target.root_dir
                target_skills_root = project_root / effective_root / "skills"

            # Dedup: skip if same resolved skills root already processed.
            resolved_root = target_skills_root.resolve()
            if resolved_root in seen_skill_dirs:
                if logger:
                    logger.progress(
                        f"{target_skills_root} -- already deployed, skipping for {target.name}",
                        symbol="info",
                    )
                continue
            seen_skill_dirs.add(resolved_root)

            target_skills_root.mkdir(parents=True, exist_ok=True)

            n, deployed = self._promote_sub_skills(
                sub_skills_dir,
                target_skills_root,
                parent_name,
                warn=is_primary,
                owned_by=owned_by if is_primary else None,
                diagnostics=diagnostics if is_primary else None,
                managed_files=managed_files if is_primary else None,
                force=force,
                project_root=project_root,
            )
            if is_primary:
                count = n
            all_deployed.extend(deployed)

        return count, all_deployed

    def _integrate_native_skill(
        self,
        package_info,
        project_root: Path,
        source_skill_md: Path,
        diagnostics=None,
        managed_files=None,
        force: bool = False,
        logger=None,
        targets=None,
    ) -> SkillIntegrationResult:
        """Copy a native Skill (with existing SKILL.md) to all active targets.

        For packages that already have a SKILL.md at their root (like those from
        awesome-claude-skills), we copy the entire skill folder to every active
        target that supports skills (driven by ``active_targets()``).

        The skill folder name is the source folder name (e.g., ``mcp-builder``),
        validated and normalized per the agentskills.io spec.

        Source SKILL.md is copied verbatim -- no metadata injection. Orphan
        detection uses apm.lock via directory name matching instead.

        Copies:
        - SKILL.md (required)
        - scripts/ (optional)
        - references/ (optional)
        - assets/ (optional)
        - Any other subdirectories the package contains

        Args:
            package_info: PackageInfo object with package metadata
            project_root: Root directory of the project
            source_skill_md: Path to the source SKILL.md file

        Returns:
            SkillIntegrationResult: Results of the integration operation
        """
        package_path = package_info.install_path

        # Use the source folder name as the skill name
        # e.g., apm_modules/ComposioHQ/awesome-claude-skills/mcp-builder -> mcp-builder
        raw_skill_name = package_path.name

        # Validate skill name per agentskills.io spec
        is_valid, error_msg = validate_skill_name(raw_skill_name)
        if is_valid:
            skill_name = raw_skill_name
        else:
            # Normalize the name if validation fails
            skill_name = normalize_skill_name(raw_skill_name)
            if diagnostics is not None:
                diagnostics.warn(
                    f"Skill name '{raw_skill_name}' normalized to '{skill_name}' ({error_msg})",
                    package=raw_skill_name,
                )
            elif logger:
                logger.warning(
                    f"Skill name '{raw_skill_name}' normalized to '{skill_name}' ({error_msg})"
                )
            else:
                try:
                    from apm_cli.utils.console import _rich_warning

                    _rich_warning(
                        f"Skill name '{raw_skill_name}' normalized to '{skill_name}' ({error_msg})"
                    )
                except ImportError:
                    pass  # CLI not available in tests

        # Deploy to all active targets that support skills.
        # When *targets* is provided (from --target), use it directly.
        # Otherwise auto-detect with copilot as the fallback.
        if targets is None:
            from apm_cli.integration.targets import active_targets

            targets = active_targets(project_root)
        skill_created = False
        skill_updated = False
        files_copied = 0
        all_target_paths: list[Path] = []
        primary_skill_md: Path | None = None

        # Read lockfile once and derive both maps in a single pass.
        owned_by, lockfile_native_owners = self._build_ownership_maps(project_root)
        sub_skills_dir = package_path / ".apm" / "skills"

        # Full unique key of the package currently being installed.
        dep_ref = package_info.dependency_ref
        current_key: str | None = dep_ref.get_unique_key() if dep_ref is not None else None

        seen_skill_dirs: set[Path] = set()

        for idx, target in enumerate(targets):
            if not target.supports("skills"):
                continue

            is_primary = idx == 0  # first active target owns diagnostics
            skills_mapping = target.primitives["skills"]
            # Dynamic-root targets (cowork): use resolved_deploy_root.
            if target.resolved_deploy_root is not None:
                target_skill_dir = target.resolved_deploy_root / skill_name
            else:
                effective_root = skills_mapping.deploy_root or target.root_dir
                target_skill_dir = project_root / effective_root / "skills" / skill_name

            # Security: validate name + containment + symlink rejection.
            from apm_cli.utils.path_security import (
                PathTraversalError,
                ensure_path_within,
                validate_path_segments,
            )

            validate_path_segments(skill_name, context="skill name")
            if target_skill_dir.is_symlink():
                raise PathTraversalError(
                    f"Skill destination {target_skill_dir} is a symlink -- refusing to deploy"
                )
            if target.resolved_deploy_root is None:
                ensure_path_within(target_skill_dir, project_root / effective_root / "skills")

            # Dedup: skip if same resolved path already deployed.
            resolved = target_skill_dir.resolve()
            if resolved in seen_skill_dirs:
                if logger:
                    logger.progress(
                        f"{target_skill_dir} -- already deployed, skipping for {target.name}",
                        symbol="info",
                    )
                continue
            seen_skill_dirs.add(resolved)

            if is_primary:
                skill_created = not target_skill_dir.exists()
                skill_updated = not skill_created
                primary_skill_md = target_skill_dir / "SKILL.md"

            if target_skill_dir.exists():
                if is_primary:
                    # Check both the lockfile (previous runs) and the in-memory session
                    # map (current run) so that same-manifest collisions are caught even
                    # before the lockfile has been written for this run.
                    prev_owner = lockfile_native_owners.get(
                        skill_name
                    ) or self._native_skill_session_owners.get(skill_name)
                    is_self_overwrite = prev_owner is not None and prev_owner == current_key
                    if prev_owner is not None and not is_self_overwrite:
                        try:
                            rel_prefix = target_skill_dir.parent.relative_to(
                                project_root
                            ).as_posix()
                        except ValueError:
                            # Dynamic-root targets (cowork): directory is
                            # outside the project tree.
                            rel_prefix = "skills"
                        rel_path = f"{rel_prefix}/{skill_name}"
                        # Issue 1: package= should identify the package causing the
                        # collision (current_key), not the skill name, so render_summary()
                        # groups diagnostics by the package responsible.
                        # Issue 2: message must tell the user what to do ("So What?" test).
                        detail = (
                            f"Skill '{skill_name}' from '{current_key}' replaced "
                            f"'{prev_owner}' -- remove one package to avoid this"
                        )
                        if diagnostics is not None:
                            diagnostics.overwrite(
                                path=rel_path,
                                package=current_key or skill_name,
                                detail=detail,
                            )
                        elif logger:
                            logger.warning(detail)
                        else:
                            # Reached when called without diagnostics or logger (e.g. uninstall sync).
                            from apm_cli.utils.console import _rich_warning

                            _rich_warning(detail)
                shutil.rmtree(target_skill_dir)

            target_skill_dir.parent.mkdir(parents=True, exist_ok=True)
            from apm_cli.security.gate import ignore_non_content

            _apm_filter = shutil.ignore_patterns(".apm")

            def _ignore_non_content_and_apm(directory, contents):
                return list(
                    set(ignore_non_content(directory, contents))
                    | set(_apm_filter(directory, contents))  # noqa: B023
                )

            shutil.copytree(package_path, target_skill_dir, ignore=_ignore_non_content_and_apm)
            all_target_paths.append(target_skill_dir)

            if is_primary:
                files_copied = sum(1 for _ in target_skill_dir.rglob("*") if _.is_file())

            # Promote sub-skills for this target
            if target.resolved_deploy_root is not None:
                target_skills_root = target.resolved_deploy_root
            else:
                target_skills_root = project_root / effective_root / "skills"
            _, sub_deployed = self._promote_sub_skills(
                sub_skills_dir,
                target_skills_root,
                skill_name,
                warn=is_primary,
                owned_by=owned_by if is_primary else None,
                diagnostics=diagnostics if is_primary else None,
                managed_files=managed_files if is_primary else None,
                force=force,
                project_root=project_root,
                logger=logger if is_primary else None,
            )
            all_target_paths.extend(sub_deployed)

        # Record ownership in the session map so subsequent packages installed in
        # the same run can detect a collision even before the lockfile is written.
        if current_key is not None:
            self._native_skill_session_owners[skill_name] = current_key

        # Count unique sub-skills from primary target only
        primary_root = project_root / ".github" / "skills"
        sub_skills_count = sum(
            1 for p in all_target_paths if p.parent == primary_root and p.name != skill_name
        )

        return SkillIntegrationResult(
            skill_created=skill_created,
            skill_updated=skill_updated,
            skill_skipped=False,
            skill_path=primary_skill_md,
            references_copied=files_copied,
            links_resolved=0,
            sub_skills_promoted=sub_skills_count,
            target_paths=all_target_paths,
        )

    def _integrate_skill_bundle(
        self,
        package_info,
        project_root: Path,
        skills_dir: Path,
        diagnostics=None,
        managed_files=None,
        force: bool = False,
        logger=None,
        targets=None,
        skill_subset=None,
    ) -> SkillIntegrationResult:
        """Promote every skill in a SKILL_BUNDLE's top-level skills/ directory.

        Reuses the same promotion logic as _promote_sub_skills but sources
        from package_root/skills/ instead of .apm/skills/.  Each nested
        skill directory becomes a top-level skill in every target.

        Args:
            package_info: PackageInfo with package metadata.
            project_root: Root directory of the project.
            skills_dir: The package's skills/ directory.
            diagnostics: Optional DiagnosticCollector.
            managed_files: Set of managed file paths.
            force: Whether to overwrite locally-authored files.
            logger: Optional InstallLogger.
            targets: Optional explicit list of TargetProfile objects.
            skill_subset: Optional tuple of skill names to install (None = all).

        Returns:
            SkillIntegrationResult with all promoted skills.
        """
        if targets is None:
            from apm_cli.integration.targets import active_targets

            targets = active_targets(project_root)

        parent_name = package_info.install_path.name
        owned_by, lockfile_native_owners = self._build_ownership_maps(project_root)  # noqa: RUF059

        total_promoted = 0
        all_deployed: list[Path] = []
        any_created = False
        seen_skill_dirs: set[Path] = set()

        # Convert skill_subset tuple to a set for O(1) lookup
        _name_filter = set(skill_subset) if skill_subset else None

        for idx, target in enumerate(targets):
            if not target.supports("skills"):
                continue

            is_primary = idx == 0
            skills_mapping = target.primitives["skills"]
            effective_root = skills_mapping.deploy_root or target.root_dir
            target_skills_root = project_root / effective_root / "skills"

            # Dedup: skip if same resolved skills root already processed.
            resolved_root = target_skills_root.resolve()
            if resolved_root in seen_skill_dirs:
                if logger:
                    logger.progress(
                        f"{target_skills_root} -- already deployed, skipping for {target.name}",
                        symbol="info",
                    )
                continue
            seen_skill_dirs.add(resolved_root)

            target_skills_root.mkdir(parents=True, exist_ok=True)

            n, deployed = self._promote_sub_skills(
                skills_dir,
                target_skills_root,
                parent_name,
                warn=is_primary,
                owned_by=owned_by if is_primary else None,
                diagnostics=diagnostics if is_primary else None,
                managed_files=managed_files if is_primary else None,
                force=force,
                project_root=project_root,
                logger=logger if is_primary else None,
                name_filter=_name_filter,
            )
            if is_primary:
                total_promoted = n
                if n > 0:
                    any_created = True
            all_deployed.extend(deployed)

        return SkillIntegrationResult(
            skill_created=any_created,
            skill_updated=False,
            skill_skipped=False,
            skill_path=None,
            references_copied=0,
            links_resolved=0,
            sub_skills_promoted=total_promoted,
            target_paths=all_deployed,
        )

    def integrate_package_skill(
        self,
        package_info,
        project_root: Path,
        diagnostics=None,
        managed_files=None,
        force: bool = False,
        logger=None,
        targets=None,
        skill_subset=None,
    ) -> SkillIntegrationResult:
        """Integrate a package's skill into all active target directories.

        Copies native skills (packages with SKILL.md at root) to every active
        target that supports skills (e.g. .github/skills/, .claude/skills/,
        .opencode/skills/). Also promotes any sub-skills from .apm/skills/.

        When *targets* is provided (e.g. from ``--target cursor``), only those
        targets are considered.  Otherwise falls back to ``active_targets()``.

        Packages without SKILL.md at root are not installed as skills -- only their
        sub-skills (if any) are promoted.

        Args:
            package_info: PackageInfo object with package metadata
            project_root: Root directory of the project
            targets: Optional explicit list of TargetProfile objects.

        Returns:
            SkillIntegrationResult: Results of the integration operation
        """
        # Check if package type allows skill installation (T4 routing)
        # SKILL and HYBRID -> install as skill
        # INSTRUCTIONS and PROMPTS -> skip skill installation
        if not should_install_skill(package_info):
            # Even non-skill packages may ship sub-skills under .apm/skills/.
            # Promote them so Copilot can discover them independently.
            sub_skills_count, sub_deployed = self._promote_sub_skills_standalone(
                package_info,
                project_root,
                diagnostics=diagnostics,
                managed_files=managed_files,
                force=force,
                logger=logger,
                targets=targets,
            )
            return SkillIntegrationResult(
                skill_created=False,
                skill_updated=False,
                skill_skipped=True,
                skill_path=None,
                references_copied=0,
                links_resolved=0,
                sub_skills_promoted=sub_skills_count,
                target_paths=sub_deployed,
            )

        # Skip virtual FILE packages - they're individual files, not full packages
        # Multiple virtual files from the same repo would collide on skill name
        # BUT: subdirectory packages (like Claude Skills) SHOULD generate skills
        if package_info.dependency_ref and package_info.dependency_ref.is_virtual:
            # Allow subdirectory packages through - they are complete skill packages
            if not package_info.dependency_ref.is_virtual_subdirectory():
                return SkillIntegrationResult(
                    skill_created=False,
                    skill_updated=False,
                    skill_skipped=True,
                    skill_path=None,
                    references_copied=0,
                    links_resolved=0,
                )

        package_path = package_info.install_path

        # Check if this is a native Skill (already has SKILL.md at root)
        source_skill_md = package_path / "SKILL.md"
        if source_skill_md.exists():
            if skill_subset:
                from apm_cli.utils.console import _rich_warning

                _rich_warning(
                    f"--skill filter ignored for '{package_info.install_path.name}': "
                    "package is a single CLAUDE_SKILL, not a SKILL_BUNDLE."
                )
            return self._integrate_native_skill(
                package_info,
                project_root,
                source_skill_md,
                diagnostics=diagnostics,
                managed_files=managed_files,
                force=force,
                logger=logger,
                targets=targets,
            )

        # SKILL_BUNDLE: promote skills from root-level skills/ directory.
        root_skills_dir = package_path / "skills"
        if root_skills_dir.is_dir() and any(
            (d / "SKILL.md").exists() for d in root_skills_dir.iterdir() if d.is_dir()
        ):
            return self._integrate_skill_bundle(
                package_info,
                project_root,
                root_skills_dir,
                diagnostics=diagnostics,
                managed_files=managed_files,
                force=force,
                logger=logger,
                targets=targets,
                skill_subset=skill_subset,
            )

        # No SKILL.md at root  -- not a skill package.
        # Still promote any sub-skills shipped under .apm/skills/.
        sub_skills_count, sub_deployed = self._promote_sub_skills_standalone(
            package_info,
            project_root,
            diagnostics=diagnostics,
            managed_files=managed_files,
            force=force,
            logger=logger,
            targets=targets,
        )
        return SkillIntegrationResult(
            skill_created=False,
            skill_updated=False,
            skill_skipped=True,
            skill_path=None,
            references_copied=0,
            links_resolved=0,
            sub_skills_promoted=sub_skills_count,
            target_paths=sub_deployed,
        )

    def sync_integration(
        self,
        apm_package,
        project_root: Path,
        managed_files: set = None,  # noqa: RUF013
        targets=None,
    ) -> dict[str, int]:
        """Sync skill directories with currently installed packages.

        Derives skill prefixes dynamically from *targets* (or
        ``KNOWN_TARGETS``) so user-scope paths like ``.copilot/skills/``
        and ``.config/opencode/skills/`` are handled correctly.

        When *managed_files* is provided, only removes skill directories
        whose paths appear in the set.  Otherwise falls back to
        npm-style orphan detection (derives expected names from installed
        dependencies).

        Args:
            apm_package: APMPackage with current dependencies
            project_root: Root directory of the project
            managed_files: Set of relative paths known to be APM-managed
            targets: Optional list of (scope-resolved) TargetProfile objects.
                     When ``None``, uses ``KNOWN_TARGETS``.

        Returns:
            Dict with cleanup statistics
        """
        from apm_cli.integration.targets import KNOWN_TARGETS

        source = targets if targets is not None else list(KNOWN_TARGETS.values())

        stats = {"files_removed": 0, "errors": 0}

        # Build the set of valid skill prefixes from targets
        skill_prefixes: list[str] = []
        for t in source:
            if not t.supports("skills"):
                continue
            # Dynamic-root targets (cowork) use cowork:// URI prefix.
            if t.user_root_resolver is not None:
                from apm_cli.integration.copilot_cowork_paths import COWORK_LOCKFILE_PREFIX

                if COWORK_LOCKFILE_PREFIX not in skill_prefixes:
                    skill_prefixes.append(COWORK_LOCKFILE_PREFIX)
                continue
            sm = t.primitives["skills"]
            effective_root = sm.deploy_root or t.root_dir
            skill_prefixes.append(f"{effective_root}/skills/")
        skill_prefix_tuple = tuple(skill_prefixes)

        if managed_files is not None:
            # Manifest-based removal -- only remove tracked skill directories
            project_root_resolved = project_root.resolve()

            # Lazy-resolve cowork root at most once per invocation
            # (mirrors the pattern in cleanup.py and sync_remove_files).
            _cowork_root_resolved: bool = False
            _cowork_root_cached: Path | None = None
            _cowork_skipped: int = 0

            for rel_path in managed_files:
                if not rel_path.startswith(skill_prefix_tuple):
                    continue
                if ".." in rel_path:
                    continue

                # Ã¢â€â‚¬Ã¢â€â‚¬ Cowork:// paths Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
                from apm_cli.integration.copilot_cowork_paths import COWORK_URI_SCHEME

                if rel_path.startswith(COWORK_URI_SCHEME):
                    try:
                        if not _cowork_root_resolved:
                            from apm_cli.integration.copilot_cowork_paths import (
                                resolve_copilot_cowork_skills_dir,
                            )

                            _cowork_root_cached = resolve_copilot_cowork_skills_dir()
                            _cowork_root_resolved = True
                        if _cowork_root_cached is None:
                            _cowork_skipped += 1
                            continue
                        from apm_cli.integration.copilot_cowork_paths import from_lockfile_path

                        target = from_lockfile_path(rel_path, _cowork_root_cached)
                    except Exception:
                        stats["errors"] += 1
                        continue
                else:
                    target = project_root / rel_path
                    if not str(target.resolve()).startswith(str(project_root_resolved)):
                        continue

                if not target.exists():
                    continue

                try:
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                    stats["files_removed"] += 1
                except Exception:
                    stats["errors"] += 1

            # One-time warning when cowork entries were skipped
            # because the OneDrive path is unavailable.
            if _cowork_skipped > 0:
                from apm_cli.utils.console import _rich_warning

                _rich_warning(
                    f"Cowork: skipping {_cowork_skipped} skill "
                    f"{'entry' if _cowork_skipped == 1 else 'entries'}"
                    " -- OneDrive path not detected.\n"
                    "Run: apm config set copilot-cowork-skills-dir <path>  "
                    "(or set APM_COPILOT_COWORK_SKILLS_DIR)\n"
                    "to clean up these entries on the next install/uninstall.",
                    symbol="warning",
                )

            return stats

        # Legacy fallback: npm-style orphan detection
        # Build set of expected skill directory names from installed packages
        installed_skill_names = set()
        for dep in apm_package.get_apm_dependencies():
            raw_name = dep.repo_url.split("/")[-1]
            if dep.is_virtual and dep.virtual_path:
                raw_name = dep.virtual_path.split("/")[-1]
            is_valid, _ = validate_skill_name(raw_name)
            skill_name = raw_name if is_valid else normalize_skill_name(raw_name)
            installed_skill_names.add(skill_name)

            # Also include promoted sub-skills from installed packages
            install_path = dep.get_install_path(project_root / "apm_modules")
            sub_skills_dir = install_path / ".apm" / "skills"
            if sub_skills_dir.is_dir():
                for sub_skill_path in sub_skills_dir.iterdir():
                    if sub_skill_path.is_dir() and (sub_skill_path / "SKILL.md").exists():
                        raw_sub = sub_skill_path.name
                        is_valid, _ = validate_skill_name(raw_sub)
                        installed_skill_names.add(
                            raw_sub if is_valid else normalize_skill_name(raw_sub)
                        )

        # Clean all target skill directories dynamically
        seen_cleanup_dirs: set[Path] = set()
        for t in source:
            if not t.supports("skills"):
                continue
            sm = t.primitives["skills"]
            effective_root = sm.deploy_root or t.root_dir

            # Special guard for cross-tool deploy_root (.agents/)
            # Only clean if the owning target dir exists
            if sm.deploy_root:
                if not (project_root / t.root_dir).is_dir():
                    continue

            skills_dir = project_root / effective_root / "skills"

            # Dedup: skip if same resolved skills dir already cleaned.
            resolved_skills = skills_dir.resolve()
            if resolved_skills in seen_cleanup_dirs:
                import logging

                logging.getLogger(__name__).debug(
                    "%s -- already processed, skipping cleanup for %s", skills_dir, t.name
                )
                continue
            seen_cleanup_dirs.add(resolved_skills)

            if skills_dir.exists():
                result = self._clean_orphaned_skills(
                    skills_dir, installed_skill_names, project_root=project_root
                )
                stats["files_removed"] += result["files_removed"]
                stats["errors"] += result["errors"]

        return stats

    def _clean_orphaned_skills(
        self,
        skills_dir: Path,
        installed_skill_names: set,
        *,
        project_root: Path | None = None,
    ) -> dict[str, int]:
        """Clean orphaned skills from a skills directory.

        Uses npm-style approach: any skill directory not matching an installed
        package name is considered orphaned and removed.

        For the cross-client ``.agents/skills/`` directory, only removes skill
        directories that appear in the lockfile's ``deployed_files`` to avoid
        deleting foreign skills placed by other tools (Codex CLI, manual).

        Args:
            skills_dir: Path to skills directory (.github/skills/, .claude/skills/, etc.)
            installed_skill_names: Set of expected skill directory names
            project_root: Project root for lockfile-based ownership check.

        Returns:
            Dict with cleanup statistics
        """
        files_removed = 0
        errors = 0

        # For .agents/skills/: only delete skills that APM owns (appear in lockfile).
        is_agents_dir = skills_dir.parent.name == ".agents"
        lockfile_owned_skills: set[str] | None = None
        if is_agents_dir and project_root is not None:
            lockfile_owned_skills = self._get_lockfile_owned_agent_skills(project_root)

        for skill_subdir in skills_dir.iterdir():
            if skill_subdir.is_dir():
                if skill_subdir.name not in installed_skill_names:
                    # Ownership check: skip foreign skills in .agents/skills/.
                    if lockfile_owned_skills is not None:
                        if skill_subdir.name not in lockfile_owned_skills:
                            continue
                    try:
                        shutil.rmtree(skill_subdir)
                        files_removed += 1
                    except Exception:
                        errors += 1

        return {"files_removed": files_removed, "errors": errors}

    @staticmethod
    def _get_lockfile_owned_agent_skills(project_root: Path) -> set[str]:
        """Return the set of skill names under ``.agents/skills/`` in the lockfile.

        Used by ``_clean_orphaned_skills`` to avoid deleting foreign skills
        in the cross-client ``.agents/`` directory.
        """
        owned: set[str] = set()
        try:
            from apm_cli.deps.lockfile import LockFile, get_lockfile_path

            lockfile = LockFile.read(get_lockfile_path(project_root))
            if lockfile and lockfile.dependencies:
                for dep in lockfile.dependencies.values():
                    for f in dep.deployed_files:
                        if f.startswith(".agents/skills/"):
                            parts = f[len(".agents/skills/") :].split("/")
                            if parts and parts[0]:
                                owned.add(parts[0])
        except (FileNotFoundError, OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
            import logging

            logging.getLogger(__name__).debug(
                "Could not read lockfile for ownership check: %s", exc
            )
        return owned
