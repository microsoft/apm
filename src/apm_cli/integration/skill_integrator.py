"""Skill integration functionality for APM packages (Claude Code & Cursor support)."""

import filecmp
import hashlib
import os
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.targets import TargetProfile
from apm_cli.utils.atomic_io import write_text_lf


def _build_copy_ignore(
    *,
    skip_bin: bool = False,
) -> Callable[[str, list[str]], list[str]]:
    """Build a ``shutil.copytree`` ignore function.

    When *skip_bin* is True the returned function also excludes ``bin/``
    directories so that unapproved executables are not deployed during
    skill promotion.
    """
    from apm_cli.security.gate import ignore_non_content

    if not skip_bin:
        return ignore_non_content
    _bin_filter = shutil.ignore_patterns("bin")

    def _combined(directory: str, contents: list[str]) -> list[str]:
        return list(
            set(ignore_non_content(directory, contents)) | set(_bin_filter(directory, contents))
        )

    return _combined


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
    bin_deployed: int = 0  # Number of marketplace_plugin bin/ executables deployed
    # Why a plugin's bin/ was NOT deployed despite shipping one, so the install
    # layer can surface an actionable hint: "project_scope" | "no_claude_target".
    bin_skipped_reason: str | None = None
    target_paths: list[Path] = None  # All deployed directories (for deployed_files manifest)

    def __post_init__(self):
        if self.target_paths is None:
            self.target_paths = []


def to_hyphen_case(name: str) -> str:
    """Convert a package name to hyphen-case for Claude Skills spec.

    Args:
        name: Package name (e.g., "owner/repo" or "MyPackage")

    Returns:
        str: Hyphen-case name, max 64 chars (e.g., "owner-repo" or "my-package")
    """
    # Extract just the repo name if it's owner/repo format
    if "/" in name:
        name = name.split("/")[-1]

    # Replace underscores and spaces with hyphens
    result = name.replace("_", "-").replace(" ", "-")

    # Insert hyphens before uppercase letters (camelCase to hyphen-case)
    result = re.sub(r"([a-z])([A-Z])", r"\1-\2", result)

    # Convert to lowercase and remove any invalid characters
    result = re.sub(r"[^a-z0-9-]", "", result.lower())

    # Remove consecutive hyphens
    result = re.sub(r"-+", "-", result)

    # Remove leading/trailing hyphens
    result = result.strip("-")

    # Truncate to 64 chars (Claude Skills spec limit)
    return result[:64]


def validate_skill_name(name: str) -> tuple[bool, str]:
    """Validate skill name per agentskills.io spec.

    Skill names must:
    - Be 1-64 characters long
    - Contain only lowercase alphanumeric characters and hyphens (a-z, 0-9, -)
    - Not contain consecutive hyphens (--)
    - Not start or end with a hyphen

    Args:
        name: Skill name to validate

    Returns:
        tuple[bool, str]: (is_valid, error_message)
            - is_valid: True if name is valid, False otherwise
            - error_message: Empty string if valid, descriptive error otherwise
    """
    # Check length
    if len(name) < 1:
        return (False, "Skill name cannot be empty")

    if len(name) > 64:
        return (False, f"Skill name must be 1-64 characters (got {len(name)})")

    # Check for consecutive hyphens
    if "--" in name:
        return (False, "Skill name cannot contain consecutive hyphens (--)")

    # Check for leading/trailing hyphens
    if name.startswith("-"):
        return (False, "Skill name cannot start with a hyphen")

    if name.endswith("-"):
        return (False, "Skill name cannot end with a hyphen")

    # Check for valid characters (lowercase alphanumeric + hyphens only)
    # Pattern: must start and end with alphanumeric, with alphanumeric or hyphens in between
    pattern = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"
    if not re.match(pattern, name):
        # Determine specific error
        if any(c.isupper() for c in name):
            return (False, "Skill name must be lowercase (no uppercase letters)")

        if "_" in name:
            return (False, "Skill name cannot contain underscores (use hyphens instead)")

        if " " in name:
            return (False, "Skill name cannot contain spaces (use hyphens instead)")

        # Check for other invalid characters
        invalid_chars = set(re.findall(r"[^a-z0-9-]", name))
        if invalid_chars:
            return (
                False,
                f"Skill name contains invalid characters: {', '.join(sorted(invalid_chars))}",
            )

        return (False, "Skill name must be lowercase alphanumeric with hyphens only")

    return (True, "")


def normalize_skill_name(name: str) -> str:
    """Convert any package name to a valid skill name per agentskills.io spec.

    Normalization steps:
    1. Extract repo name if owner/repo format
    2. Convert to lowercase
    3. Replace underscores and spaces with hyphens
    4. Convert camelCase to hyphen-case
    5. Remove invalid characters
    6. Remove consecutive hyphens
    7. Strip leading/trailing hyphens
    8. Truncate to 64 characters

    Args:
        name: Package name to normalize (e.g., "owner/MyRepo_Name")

    Returns:
        str: Valid skill name (e.g., "my-repo-name")
    """
    # Use to_hyphen_case which already handles most normalization
    return to_hyphen_case(name)


# =============================================================================
# Package Type Routing Functions (T4)
# =============================================================================
# These functions determine behavior based on:
# 1. Explicit `type` field in apm.yml (highest priority)
# 2. Presence of SKILL.md at package root (makes it a skill)
# 3. Default to INSTRUCTIONS for instruction-only packages
#
# Per skill-strategy.md Decision 2: "Skills are explicit, not implicit"
# - Packages with SKILL.md OR explicit type: skill/hybrid -> become skills
# - Packages with only instructions -> compile to AGENTS.md, NOT skills


def get_effective_type(package_info) -> "PackageContentType":
    """Get effective package content type based on package structure.

    Determines type by:
    1. Package has SKILL.md (PackageType.CLAUDE_SKILL or HYBRID) -> SKILL
    2. Package is a SKILL_BUNDLE or MARKETPLACE_PLUGIN (has skills/) -> SKILL
    3. Otherwise -> INSTRUCTIONS (compile to AGENTS.md only)

    Args:
        package_info: PackageInfo object containing package metadata

    Returns:
        PackageContentType: The effective type
    """
    from apm_cli.models.apm_package import PackageContentType, PackageType

    # Check if package has SKILL.md (via package_type field)
    # PackageType.CLAUDE_SKILL = has root SKILL.md only
    # PackageType.HYBRID = has both apm.yml AND root SKILL.md
    # PackageType.SKILL_BUNDLE = has skills/<name>/SKILL.md (nested bundle)
    # PackageType.MARKETPLACE_PLUGIN = has plugin manifest (plugin.json or
    #   .claude-plugin/); may or may not include skills/. The integrator
    #   path gates on actual skills/ presence, so plugins without skills
    #   are inert in the SKILL branch.
    if package_info.package_type in (
        PackageType.CLAUDE_SKILL,
        PackageType.HYBRID,
        PackageType.SKILL_BUNDLE,
        PackageType.MARKETPLACE_PLUGIN,
    ):
        return PackageContentType.SKILL

    # Default to INSTRUCTIONS for packages without SKILL.md
    return PackageContentType.INSTRUCTIONS


def should_install_skill(package_info) -> bool:
    """Determine if package should be installed as a native skill.

    This controls whether a package gets installed to .github/skills/ (or .claude/skills/).

    Per skill-strategy.md Decision 2 - "Skills are explicit, not implicit":

    Returns True for:
        - SKILL: Package has SKILL.md or declares type: skill
        - HYBRID: Package declares type: hybrid in apm.yml

    Returns False for:
        - INSTRUCTIONS: Compile to AGENTS.md only, no skill created
        - PROMPTS: Commands/prompts only, no skill created
        - Packages without SKILL.md and no explicit type field

    Args:
        package_info: PackageInfo object containing package metadata

    Returns:
        bool: True if package should be installed as a native skill
    """
    from apm_cli.models.apm_package import PackageContentType

    effective_type = get_effective_type(package_info)

    # SKILL and HYBRID should install as skills
    # INSTRUCTIONS and PROMPTS should NOT install as skills
    return effective_type in (PackageContentType.SKILL, PackageContentType.HYBRID)


def should_compile_instructions(package_info) -> bool:
    """Determine if package should compile to AGENTS.md/CLAUDE.md.

    This controls whether a package's instructions are included in compiled output.

    Per skill-strategy.md Decision 2:

    Returns True for:
        - INSTRUCTIONS: Compile to AGENTS.md only (default for packages without SKILL.md)
        - HYBRID: Package declares type: hybrid in apm.yml

    Returns False for:
        - SKILL: Install as native skill only, no AGENTS.md compilation
        - PROMPTS: Commands/prompts only, no instructions compiled

    Args:
        package_info: PackageInfo object containing package metadata

    Returns:
        bool: True if package's instructions should be compiled to AGENTS.md/CLAUDE.md
    """
    from apm_cli.models.apm_package import PackageContentType

    effective_type = get_effective_type(package_info)

    # INSTRUCTIONS and HYBRID should compile to AGENTS.md
    # SKILL and PROMPTS should NOT compile to AGENTS.md
    return effective_type in (PackageContentType.INSTRUCTIONS, PackageContentType.HYBRID)


def copy_skill_to_target(
    package_info,
    source_path: Path,
    target_base: Path,
    targets=None,
) -> list[Path]:
    """Copy skill directory to all active target skills/ directories.

    This is a standalone function for direct skill copy operations.
    It handles:
    - Package type routing via should_install_skill()
    - Skill name validation/normalization
    - Directory structure preservation
    - Deployment to every active target that supports skills

    When *targets* is provided, only those targets are used.
    Otherwise falls back to ``active_targets()``.

    Source SKILL.md gets no metadata injection; outbound package links are rewritten.

    Copies:
    - SKILL.md (required)
    - scripts/ (optional)
    - references/ (optional)
    - assets/ (optional)
    - Any other subdirectories the package contains

    Args:
        package_info: PackageInfo object with package metadata
        source_path: Path to skill in apm_modules/
        target_base: Usually project root
        targets: Optional explicit list of TargetProfile objects.

    Returns:
        List of all deployed skill directory paths (empty if skipped).
    """
    # Check if package type allows skill installation (T4 routing)
    if not should_install_skill(package_info):
        return []

    # Check for SKILL.md existence
    source_skill_md = source_path / "SKILL.md"
    if not source_skill_md.exists():
        # No SKILL.md means this package is handled by compilation, not skill copy
        return []

    # Get and validate skill name from folder
    raw_skill_name = source_path.name

    is_valid, _ = validate_skill_name(raw_skill_name)
    if is_valid:  # noqa: SIM108
        skill_name = raw_skill_name
    else:
        skill_name = normalize_skill_name(raw_skill_name)

    deployed: list[Path] = []
    seen_skill_dirs: set[Path] = set()

    # Deploy to all active targets that support skills.
    # When no targets are provided, fall back to project-scope detection.
    # Callers responsible for user-scope should pass resolved targets
    # from resolve_targets().
    if targets is None:
        from apm_cli.integration.targets import active_targets

        targets = active_targets(target_base)
    for target in targets:
        if not target.supports("skills"):
            continue
        skills_mapping = target.primitives["skills"]
        effective_root = skills_mapping.deploy_root or target.root_dir

        # Skip if target dir does not exist and auto_create is disabled
        target_root_dir = target_base / target.root_dir
        if not target.auto_create and not target_root_dir.is_dir():
            continue

        skill_dir = target_base / effective_root / "skills" / skill_name

        # Security: reject traversal in skill name and validate containment.
        # The containment check resolves the *base* (which may sit behind a
        # symlink) but verifies the *unresolved* caller-controlled segment
        # (skill_name) has no traversal parts.  This prevents a symlink at
        # target_base / effective_root from silently redirecting writes
        # outside the project root.
        from apm_cli.utils.path_security import (
            PathTraversalError,
            ensure_path_within,
            validate_path_segments,
        )

        validate_path_segments(skill_name, context="skill name")
        if skill_dir.is_symlink():
            raise PathTraversalError(
                f"Skill destination {skill_dir} is a symlink -- refusing to deploy"
            )

        # Verify the resolved skill directory is within the project root.
        # This catches the case where an ancestor directory (e.g.
        # effective_root) is a symlink pointing outside the project.
        resolved_project = target_base.resolve()
        resolved_skill_dir = skill_dir.resolve()
        if not resolved_skill_dir.is_relative_to(resolved_project):
            raise PathTraversalError(
                f"Skill directory '{skill_dir}' resolves to '{resolved_skill_dir}' "
                f"which is outside the project root '{resolved_project}'"
            )
        ensure_path_within(skill_dir, target_base / effective_root / "skills")

        # Dedup: skip if same resolved path already deployed.
        resolved = skill_dir.resolve()
        if resolved in seen_skill_dirs:
            import logging

            logging.getLogger(__name__).debug(
                "%s -- already deployed, skipping for %s", skill_dir, target.name
            )
            continue
        seen_skill_dirs.add(resolved)

        skill_dir.parent.mkdir(parents=True, exist_ok=True)
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        from apm_cli.security.gate import ignore_non_content

        shutil.copytree(source_path, skill_dir, ignore=ignore_non_content)
        rewriter = SkillIntegrator()
        rewriter.init_link_resolver(package_info, target_base)
        rewriter._resolve_markdown_links_in_skill_bundle(source_path, skill_dir)
        deployed.append(skill_dir)

    return deployed


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

    @staticmethod
    def _target_skills_root(target: TargetProfile, project_root: Path) -> Path:
        """Return the target skills root for static and dynamic-root targets."""
        if target.resolved_deploy_root is not None:
            return target.deploy_path(project_root)
        skills_mapping = target.primitives["skills"]
        effective_root = skills_mapping.deploy_root or target.root_dir
        return project_root / effective_root / "skills"

    @staticmethod
    def _target_skill_dir(target: TargetProfile, project_root: Path, skill_name: str) -> Path:
        """Return the concrete directory for a deployed skill."""
        return SkillIntegrator._target_skills_root(target, project_root) / skill_name

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
    def is_skill_dir_identical_to_source(dir_a: Path, dir_b: Path) -> bool:
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

    def _resolve_markdown_links_in_skill_bundle(
        self,
        source_root: Path,
        target_root: Path,
    ) -> int:
        """Read copied skill markdown from source and write resolved target content."""
        links_resolved = 0
        for target_file in target_root.rglob("*.md"):
            if not target_file.is_file() or target_file.is_symlink():
                continue
            source_file = source_root / target_file.relative_to(target_root)
            if not source_file.is_file() or source_file.is_symlink():
                continue
            content = source_file.read_text(encoding="utf-8")
            resolved, count = self.resolve_links(
                content,
                source_file,
                target_file,
                preserved_source_root=source_root,
            )
            if count:
                write_text_lf(target_file, resolved)
                links_resolved += count
        return links_resolved

    @staticmethod
    def _skill_subset_name_filter(skill_subset: tuple[str, ...] | None) -> set[str] | None:
        """Return promotion filter tokens for --skill subset values."""
        if not skill_subset:
            return None

        name_filter: set[str] = set()
        for skill_name in skill_subset:
            raw_name = str(skill_name).strip()
            if not raw_name:
                continue
            normalized_path = raw_name.replace("\\", "/")
            leaf_name = Path(normalized_path).name
            name_filter.add(raw_name)
            name_filter.add(normalized_path)
            if leaf_name:
                name_filter.add(leaf_name)
        return name_filter or None

    @staticmethod
    def _promote_sub_skills(
        sub_skills_dir: Path,
        target_skills_root: Path,
        parent_name: str,
        *,
        warn: bool = True,
        skip_bin: bool = False,
        owned_by: dict[str, str] | None = None,
        diagnostics=None,
        managed_files=None,
        force: bool = False,
        project_root: Path | None = None,
        logger=None,
        name_filter: set[str] | None = None,
        link_rewriter: "SkillIntegrator | None" = None,
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
                # Content-identical: skip entirely (no copy, no warning)
                if SkillIntegrator.is_skill_dir_identical_to_source(sub_skill_path, target):
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
                    # User-authored skill: respect force flag
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
                        continue  # SKIP: protect user content

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
            shutil.copytree(
                sub_skill_path,
                target,
                dirs_exist_ok=True,
                ignore=_build_copy_ignore(skip_bin=skip_bin),
            )
            if link_rewriter is not None:
                link_rewriter._resolve_markdown_links_in_skill_bundle(sub_skill_path, target)
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
        skill_subset=None,
        skip_bin: bool = False,
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
            skill_subset: Optional tuple of skill names or paths to install (None = all).

        Returns:
            tuple[int, list[Path]]: (count of promoted sub-skills, list of deployed dirs)
        """
        self.init_link_resolver(package_info, project_root)
        package_path = package_info.install_path
        sub_skills_dir = package_path / ".apm" / "skills"
        if not sub_skills_dir.is_dir():
            return 0, []

        if targets is None:
            from apm_cli.integration.targets import active_targets

            targets = active_targets(project_root)

        parent_name = package_path.name
        owned_by = self._build_skill_ownership_map(project_root)
        name_filter = self._skill_subset_name_filter(skill_subset)
        count = 0
        all_deployed: list[Path] = []
        seen_skill_dirs: set[Path] = set()

        for idx, target in enumerate(targets):
            if not target.supports("skills"):
                continue

            is_primary = idx == 0  # first active target owns diagnostics
            target_skills_root = self._target_skills_root(target, project_root)

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
                name_filter=name_filter,
                link_rewriter=self,
                skip_bin=skip_bin,
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
        skip_bin: bool = False,
    ) -> SkillIntegrationResult:
        """Copy a native Skill (with existing SKILL.md) to all active targets.

        For packages that already have a SKILL.md at their root (like those from
        awesome-claude-skills), we copy the entire skill folder to every active
        target that supports skills (driven by ``active_targets()``).

        The skill folder name is the source folder name (e.g., ``mcp-builder``),
        validated and normalized per the agentskills.io spec.

        Source SKILL.md gets no metadata injection; outbound package links are rewritten.
        Orphan detection uses apm.lock via directory name matching instead.

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
        self.init_link_resolver(package_info, project_root)
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
            # Static targets still need the effective root for the containment guard below.
            effective_root = skills_mapping.deploy_root or target.root_dir
            target_skill_dir = self._target_skill_dir(target, project_root, skill_name)

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
            _base_ignore = _build_copy_ignore(skip_bin=skip_bin)

            _apm_filter = shutil.ignore_patterns(".apm")

            def _ignore_non_content_and_apm(directory, contents):
                return list(
                    set(_base_ignore(directory, contents))  # noqa: B023
                    | set(_apm_filter(directory, contents))  # noqa: B023
                )

            shutil.copytree(package_path, target_skill_dir, ignore=_ignore_non_content_and_apm)
            self._resolve_markdown_links_in_skill_bundle(package_path, target_skill_dir)
            all_target_paths.append(target_skill_dir)

            if is_primary:
                files_copied = sum(1 for _ in target_skill_dir.rglob("*") if _.is_file())

            # Promote sub-skills for this target.
            target_skills_root = self._target_skills_root(target, project_root)
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
                link_rewriter=self,
                skip_bin=skip_bin,
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
        skip_bin: bool = False,
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
        self.init_link_resolver(package_info, project_root)
        if targets is None:
            from apm_cli.integration.targets import active_targets

            targets = active_targets(project_root)

        parent_name = package_info.install_path.name
        owned_by, lockfile_native_owners = self._build_ownership_maps(project_root)  # noqa: RUF059

        total_promoted = 0
        all_deployed: list[Path] = []
        any_created = False
        seen_skill_dirs: set[Path] = set()

        # Convert skill_subset tuple to promotion filter tokens for O(1) lookup.
        _name_filter = self._skill_subset_name_filter(skill_subset)

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
                link_rewriter=self,
                skip_bin=skip_bin,
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
        scope=None,
        policy=None,
        skip_bin: bool = False,
        bin_skip_reason_override: str | None = None,
        trust_bin: bool | None = None,
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
            skill_subset: Optional tuple of skill names or paths to install (None = all).
            skip_bin: When True, skip bin/ executable deployment even if the
                package ships one.  Used by the executable approval gate to
                block unapproved bin/ executables while still deploying text
                primitives (skills, sub-skills).
            bin_skip_reason_override: When *skip_bin* is True, use this
                string as ``bin_skipped_reason`` instead of the default
                ``"not_approved"``.  Lets the caller distinguish
                ``--no-trust-bin`` (``"not_trusted"``) from the
                ``allowExecutables`` gate.
            trust_bin: Tri-state consent flag.  ``True`` suppresses the
                trust-posture warning; ``False`` is handled upstream by
                setting *skip_bin*; ``None`` (default) emits the warning
                when bin/ is deployed.

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
                skill_subset=skill_subset,
                skip_bin=skip_bin,
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

        # MARKETPLACE_PLUGIN: deploy bin/ executables + plugin manifest BEFORE
        # skill routing.  bin/ deployment is orthogonal to whether the plugin
        # also ships a root SKILL.md or a skills/ bundle, so it must run for
        # every plugin -- not only the no-skill fallback.  See issue #1544.
        bin_paths: list[Path] = []
        bin_skip_reason: str | None = None
        from apm_cli.models.apm_package import PackageType as _PackageType

        if package_info.package_type == _PackageType.MARKETPLACE_PLUGIN:
            if skip_bin:
                bin_skip_reason = bin_skip_reason_override or "not_approved"
            else:
                bin_paths, bin_skip_reason = self._deploy_plugin_bin(
                    package_info,
                    project_root,
                    targets,
                    scope=scope,
                    policy=policy,
                    force=force,
                    logger=logger,
                    trust_bin=trust_bin,
                )

        # Check if this is a native Skill (already has SKILL.md at root)
        source_skill_md = package_path / "SKILL.md"
        if source_skill_md.exists():
            if skill_subset:
                from apm_cli.utils.console import _rich_warning

                _rich_warning(
                    f"--skill filter ignored for '{package_info.install_path.name}': "
                    "package is a single CLAUDE_SKILL, not a SKILL_BUNDLE."
                )
            return self._merge_bin_paths(
                self._integrate_native_skill(
                    package_info,
                    project_root,
                    source_skill_md,
                    diagnostics=diagnostics,
                    managed_files=managed_files,
                    force=force,
                    logger=logger,
                    targets=targets,
                    skip_bin=skip_bin,
                ),
                bin_paths,
                bin_skip_reason,
            )

        # SKILL_BUNDLE: promote skills from root-level skills/ directory.
        root_skills_dir = package_path / "skills"
        if root_skills_dir.is_dir() and any(
            (d / "SKILL.md").exists() for d in root_skills_dir.iterdir() if d.is_dir()
        ):
            return self._merge_bin_paths(
                self._integrate_skill_bundle(
                    package_info,
                    project_root,
                    root_skills_dir,
                    diagnostics=diagnostics,
                    managed_files=managed_files,
                    force=force,
                    logger=logger,
                    targets=targets,
                    skill_subset=skill_subset,
                    skip_bin=skip_bin,
                ),
                bin_paths,
                bin_skip_reason,
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
            skill_subset=skill_subset,
            skip_bin=skip_bin,
        )
        return self._merge_bin_paths(
            SkillIntegrationResult(
                skill_created=False,
                skill_updated=False,
                skill_skipped=True,
                skill_path=None,
                references_copied=0,
                links_resolved=0,
                sub_skills_promoted=sub_skills_count,
                target_paths=sub_deployed,
            ),
            bin_paths,
            bin_skip_reason,
        )

    @staticmethod
    def _merge_bin_paths(
        result: SkillIntegrationResult,
        bin_paths: list[Path],
        skip_reason: str | None = None,
    ) -> SkillIntegrationResult:
        """Fold deployed plugin bin/manifest paths into a skill result.

        Pure: returns a NEW result via ``dataclasses.replace`` rather than
        mutating the argument, so callers never observe surprise in-place
        edits.  ``skill_created`` is intentionally left untouched -- deploying
        executables is not the same as creating a skill, so reporting and
        sync semantics stay honest.  When bins were deployed the result is no
        longer "skipped" (work happened) and the paths are tracked for the
        lockfile / uninstall manifest.  ``skip_reason`` records why a plugin
        that ships bin/ was NOT deployed, for the install layer to surface.
        """
        if not bin_paths and skip_reason is None:
            return result
        updates: dict = {}
        if bin_paths:
            updates["bin_deployed"] = len(bin_paths)
            updates["skill_skipped"] = False
            updates["target_paths"] = (result.target_paths or []) + bin_paths
        if skip_reason is not None:
            updates["bin_skipped_reason"] = skip_reason
        return replace(result, **updates)

    def _deploy_plugin_bin(
        self,
        package_info,
        project_root: Path,
        targets,
        scope=None,
        policy=None,
        force: bool = False,
        logger=None,
        trust_bin: bool | None = None,
    ) -> tuple[list[Path], str | None]:
        """Deploy bin/ executables and plugin manifest for a MARKETPLACE_PLUGIN.

        Only activates when ALL of:
        - The package has a bin/ directory
        - At least one Claude target that supports skills is active
        - scope is InstallScope.USER (bin/ deploy is user-scope only, v1)
        - policy does not deny the package

        This realizes Claude Code's "skills-directory plugin" contract: a folder
        under a skills directory containing ``.claude-plugin/plugin.json`` is
        loaded as ``<name>@skills-dir`` and its root ``bin/`` is added to the
        Bash tool PATH.  The contract is Claude-specific by design; other
        harnesses have no equivalent, so only Claude targets are considered.

        Each binary is tightened to user-only ``0o700``-style permissions
        (owner read/write/execute; all group/other bits cleared) on POSIX
        systems.  The deployed root is user-scoped (~/.claude/skills/), so
        tighter-than-0o755 permissions are correct.

        When *trust_bin* is ``None`` (no ``--trust-bin`` / ``--no-trust-bin``
        flag), a prominent trust-posture warning is emitted after deployment
        so the user is aware that executables are on Claude Code's PATH.
        When *trust_bin* is ``True`` (``--trust-bin``), the warning is
        suppressed.  ``False`` is never passed here (handled upstream by
        the caller setting ``skip_bin=True``).

        Returns ``(deployed_paths, skip_reason)``.  ``skip_reason`` is non-None
        ONLY when the package ships a bin/ but it could not be deployed for an
        actionable reason ("project_scope", "no_claude_target"), so the install
        layer can surface a hint.  Policy-deny and "no bin/ at all" return
        ``None`` -- they are intentional, not traps.
        """
        from apm_cli.core.scope import InstallScope
        from apm_cli.utils.path_security import validate_path_segments

        bin_dir = package_info.install_path / "bin"
        if not bin_dir.is_dir():
            return [], None

        # The package ships executables -- from here a non-deploy is a
        # reportable skip, not a silent no-op.
        if scope is not InstallScope.USER:
            if logger and scope is InstallScope.PROJECT:
                logger.progress(
                    "bin/ deploy is user-scope only; skipping for project-scope install",
                    symbol="info",
                )
            return [], "project_scope"

        if self._bin_deploy_denied(package_info, policy, logger):
            return [], None

        if targets is None:
            from apm_cli.integration.targets import active_targets

            targets = active_targets(project_root)

        # Claude-specific contract: only Claude targets that support skills.
        claude_targets = [t for t in targets if t.name == "claude" and t.supports("skills")]
        if not claude_targets:
            if logger:
                logger.progress(
                    "bin/ present but no active Claude skills target; skipping bin deploy for "
                    f"{package_info.get_canonical_dependency_string()}",
                    symbol="warning",
                )
            return [], "no_claude_target"

        skill_name = package_info.install_path.name
        validate_path_segments(skill_name, context="plugin skill name")
        deployed: list[Path] = []

        for target in claude_targets:
            effective_root = target.primitives["skills"].deploy_root or target.root_dir
            target_root_dir = project_root / target.root_dir
            if not target.auto_create and not target_root_dir.is_dir():
                continue

            skill_base = project_root / effective_root / "skills" / skill_name
            rel_prefix = f"{effective_root}/skills/{skill_name}"
            deployed.extend(self._deploy_bin_files(bin_dir, skill_base, rel_prefix, force, logger))
            manifest = self._deploy_plugin_manifest(
                package_info.install_path, skill_base, rel_prefix, force, logger
            )
            if manifest is not None:
                deployed.append(manifest)

        # Trust-posture warning: when bin/ is deployed without explicit
        # --trust-bin consent, surface a prominent warning so the user
        # knows executables are on Claude Code's PATH.
        if deployed and trust_bin is None and logger:
            canonical = package_info.get_canonical_dependency_string()
            logger.progress(
                f"[!] Plugin '{canonical}' deploys executables to Claude Code's PATH "
                "(invoked without confirmation). "
                "Pass --trust-bin to acknowledge, or --no-trust-bin to skip bin/ deployment.",
                symbol="warning",
            )

        return deployed, None

    @staticmethod
    def _bin_deploy_denied(package_info, policy, logger) -> bool:
        """Return True when policy opts the package out of bin/ deployment."""
        if policy is None:
            return False
        bd_policy = policy.bin_deploy
        if bd_policy is None:
            return False
        from apm_cli.security.executables import normalize_bin_deploy_deny_key

        canonical = package_info.get_canonical_dependency_string()
        normalized_canonical = normalize_bin_deploy_deny_key(canonical)
        if bd_policy.deny_all:
            if logger:
                logger.progress(
                    f"bin_deploy.deny_all: skipping bin deploy for {canonical}",
                    symbol="info",
                )
            return True
        deny_entries = {normalize_bin_deploy_deny_key(entry) for entry in bd_policy.deny}
        if normalized_canonical in deny_entries:
            if logger:
                logger.progress(
                    f"bin_deploy.deny: skipping bin deploy for {canonical}",
                    symbol="info",
                )
            return True
        return False

    def _deploy_bin_files(
        self,
        bin_dir: Path,
        skill_base: Path,
        rel_prefix: str,
        force: bool,
        logger,
    ) -> list[Path]:
        """Copy bin/ executables into ``skill_base/bin`` (chmod +x on POSIX)."""
        from apm_cli.utils.path_security import ensure_path_within

        dest_bin = skill_base / "bin"
        dest_bin.mkdir(parents=True, exist_ok=True)
        deployed: list[Path] = []
        for src_file in bin_dir.iterdir():
            # Reject symlinks -- a malicious package could point a symlink
            # at an arbitrary file outside the sandbox.
            if src_file.is_symlink() or not src_file.is_file():
                continue
            dest_file = dest_bin / src_file.name
            ensure_path_within(dest_file, dest_bin)
            self._copy_plugin_file(
                src_file,
                dest_file,
                force=force,
                make_executable=True,
                logger=logger,
                rel_label=f"{rel_prefix}/bin/{src_file.name}",
            )
            deployed.append(dest_file)
        return deployed

    def _deploy_plugin_manifest(
        self,
        package_path: Path,
        skill_base: Path,
        rel_prefix: str,
        force: bool,
        logger,
    ) -> Path | None:
        """Copy ``.claude-plugin/plugin.json`` next to the deployed bin/."""
        plugin_manifest = package_path / ".claude-plugin" / "plugin.json"
        if plugin_manifest.is_symlink() or not plugin_manifest.is_file():
            return None
        dest_manifest = skill_base / ".claude-plugin" / "plugin.json"
        dest_manifest.parent.mkdir(parents=True, exist_ok=True)
        self._copy_plugin_file(
            plugin_manifest,
            dest_manifest,
            force=force,
            make_executable=False,
            logger=logger,
            rel_label=f"{rel_prefix}/.claude-plugin/plugin.json",
        )
        return dest_manifest

    @staticmethod
    def _copy_plugin_file(
        src_file: Path,
        dest_file: Path,
        *,
        force: bool,
        make_executable: bool,
        logger,
        rel_label: str,
    ) -> None:
        """Hash-gated copy of one plugin file, optionally marking it executable.

        Skips the copy when an identical file already exists (unless *force*),
        keeping repeated installs quiet and idempotent.

        When *make_executable* is True, the deployed file is tightened to
        user-only ``0o700``-style permissions: the owner read/write/execute
        bits are set and every group and other bit is cleared.  Deployed
        files live under ~/.claude/skills/ which is user-scoped, so there is
        no reason to grant any group/other access regardless of what the
        source package shipped.
        """
        skip_copy = False
        if dest_file.exists() and not force:
            src_hash = hashlib.sha256(src_file.read_bytes()).hexdigest()
            dst_hash = hashlib.sha256(dest_file.read_bytes()).hexdigest()
            skip_copy = src_hash == dst_hash

        if not skip_copy:
            shutil.copy2(src_file, dest_file)

        if make_executable and os.name == "posix":
            # User-only (0o700-style): set the exact deploy mode so group,
            # other, and special bits never survive from the source package.
            # Runs for both fresh copies and idempotent re-installs so files
            # previously deployed by older APM versions are hardened in-place.
            dest_file.chmod(stat.S_IRWXU)

        if not skip_copy and logger:
            logger.progress(f"deployed {src_file.name} -> {rel_label}", symbol="check")

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

                # Cowork:// paths
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
