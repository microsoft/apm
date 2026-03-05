"""Skill integration functionality for APM packages (Claude Code support)."""

from pathlib import Path
from typing import List, Dict, TYPE_CHECKING
from dataclasses import dataclass
from datetime import datetime
import hashlib
import shutil
import re

import frontmatter

if TYPE_CHECKING:
    from apm_cli.models.apm_package import PackageContentType


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
            return (
                False,
                "Skill name cannot contain underscores (use hyphens instead)",
            )

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
# - Packages with SKILL.md OR explicit type: skill/hybrid → become skills
# - Packages with only instructions → compile to AGENTS.md, NOT skills


def get_effective_type(package_info) -> "PackageContentType":
    """Get effective package content type based on package structure.

    Determines type by:
    1. Package has SKILL.md (PackageType.CLAUDE_SKILL or HYBRID) → SKILL
    2. Otherwise → INSTRUCTIONS (compile to AGENTS.md only)

    Args:
        package_info: PackageInfo object containing package metadata

    Returns:
        PackageContentType: The effective type
    """
    from apm_cli.models.apm_package import PackageContentType, PackageType

    # Check if package has SKILL.md (via package_type field)
    # PackageType.CLAUDE_SKILL = has SKILL.md only
    # PackageType.HYBRID = has both apm.yml AND SKILL.md
    if package_info.package_type in (PackageType.CLAUDE_SKILL, PackageType.HYBRID):
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
    return effective_type in (
        PackageContentType.INSTRUCTIONS,
        PackageContentType.HYBRID,
    )


def copy_skill_to_target(
    package_info,
    source_path: Path,
    target_base: Path,
) -> tuple[Path | None, Path | None]:
    """Copy skill directory to .github/skills/ and optionally .claude/skills/.

    This is a standalone function for direct skill copy operations.
    It handles:
    - Package type routing via should_install_skill()
    - Skill name validation/normalization
    - Directory structure preservation
    - Compatibility copy to .claude/skills/ when .claude/ exists (T7)

    Source SKILL.md is copied verbatim — no metadata injection.

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

    Returns:
        Tuple of (github_path, claude_path):
        - github_path: Path to .github/skills/{name}/ or None if skipped
        - claude_path: Path to .claude/skills/{name}/ or None if .claude/ doesn't exist
    """
    # Check if package type allows skill installation (T4 routing)
    if not should_install_skill(package_info):
        return (None, None)

    # Check for SKILL.md existence
    source_skill_md = source_path / "SKILL.md"
    if not source_skill_md.exists():
        # No SKILL.md means this package is handled by compilation, not skill copy
        return (None, None)

    # Get and validate skill name from folder
    raw_skill_name = source_path.name

    is_valid, error_msg = validate_skill_name(raw_skill_name)
    if is_valid:
        skill_name = raw_skill_name
    else:
        skill_name = normalize_skill_name(raw_skill_name)

    # === Primary target: .github/skills/ ===
    github_skill_dir = target_base / ".github" / "skills" / skill_name

    # Create .github/skills/ if it doesn't exist
    github_skill_dir.parent.mkdir(parents=True, exist_ok=True)

    # If skill already exists, remove it for update
    if github_skill_dir.exists():
        shutil.rmtree(github_skill_dir)

    # Copy the entire skill folder preserving structure
    # This copies SKILL.md, scripts/, references/, assets/, etc.
    shutil.copytree(source_path, github_skill_dir)

    # === Secondary target: .claude/skills/ (T7 - compatibility copy) ===
    claude_skill_dir: Path | None = None
    claude_dir = target_base / ".claude"

    # Only copy to .claude/skills/ if .claude/ directory already exists
    # Do NOT create .claude/ folder if it doesn't exist
    if claude_dir.exists() and claude_dir.is_dir():
        claude_skill_dir = claude_dir / "skills" / skill_name

        # Create .claude/skills/ if needed (but .claude/ must already exist)
        claude_skill_dir.parent.mkdir(parents=True, exist_ok=True)

        # If skill already exists, remove it for update
        if claude_skill_dir.exists():
            shutil.rmtree(claude_skill_dir)

        # Copy the entire skill folder (identical to github copy)
        shutil.copytree(source_path, claude_skill_dir)

    # === Secondary target: .opencode/skills/ (OpenCode compatibility copy) ===
    opencode_skill_dir: Path | None = None
    opencode_dir = target_base / ".opencode"
    if opencode_dir.exists() and opencode_dir.is_dir():
        opencode_skill_dir = opencode_dir / "skills" / skill_name

        opencode_skill_dir.parent.mkdir(parents=True, exist_ok=True)

        if opencode_skill_dir.exists():
            shutil.rmtree(opencode_skill_dir)

        shutil.copytree(source_path, opencode_skill_dir)

    return (github_skill_dir, claude_skill_dir)


class SkillIntegrator:
    """Handles integration of native SKILL.md files for Claude Code and VS Code.

    Claude Skills Spec:
    - SKILL.md files provide structured context for Claude Code
    - YAML frontmatter with name, description, and metadata
    - Markdown body with instructions and agent definitions
    - references/ subdirectory for prompt files
    """

    def __init__(self):
        """Initialize the skill integrator."""
        self.link_resolver = None  # Lazy init when needed

    def should_integrate(self, project_root: Path) -> bool:
        """Check if skill integration should be performed.

        Args:
            project_root: Root directory of the project

        Returns:
            bool: Always True - integration happens automatically
        """
        return True

    def find_instruction_files(self, package_path: Path) -> List[Path]:
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

    def find_agent_files(self, package_path: Path) -> List[Path]:
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

    def find_prompt_files(self, package_path: Path) -> List[Path]:
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

    def find_context_files(self, package_path: Path) -> List[Path]:
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
    def _promote_sub_skills(
        sub_skills_dir: Path,
        target_skills_root: Path,
        parent_name: str,
        *,
        warn: bool = True,
    ) -> int:
        """Promote sub-skills from .apm/skills/ to top-level skill entries.

        Args:
            sub_skills_dir: Path to the .apm/skills/ directory in the source package.
            target_skills_root: Root skills directory (e.g. .github/skills/ or .claude/skills/).
            parent_name: Name of the parent skill (used in warning messages).
            warn: Whether to emit a warning on name collisions.

        Returns:
            int: Number of sub-skills promoted.
        """
        promoted = 0
        if not sub_skills_dir.is_dir():
            return promoted
        for sub_skill_path in sub_skills_dir.iterdir():
            if not sub_skill_path.is_dir():
                continue
            if not (sub_skill_path / "SKILL.md").exists():
                continue
            raw_sub_name = sub_skill_path.name
            is_valid, _ = validate_skill_name(raw_sub_name)
            sub_name = raw_sub_name if is_valid else normalize_skill_name(raw_sub_name)
            target = target_skills_root / sub_name
            if target.exists():
                if warn:
                    try:
                        from apm_cli.cli import _rich_warning

                        _rich_warning(
                            f"Sub-skill '{sub_name}' from '{parent_name}' overwrites existing skill at {target_skills_root.name}/{sub_name}/"
                        )
                    except ImportError:
                        pass
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
            shutil.copytree(sub_skill_path, target, dirs_exist_ok=True)
            promoted += 1
        return promoted

    def _promote_sub_skills_standalone(self, package_info, project_root: Path) -> int:
        """Promote sub-skills from a package that is NOT itself a skill.

        Packages typed as INSTRUCTIONS may still ship sub-skills under
        ``.apm/skills/``.  This method promotes them to ``.github/skills/``
        (and ``.claude/skills/`` when present) without creating a top-level
        skill entry for the parent package.

        Args:
            package_info: PackageInfo object with package metadata.
            project_root: Root directory of the project.

        Returns:
            int: Number of sub-skills promoted.
        """
        package_path = package_info.install_path
        sub_skills_dir = package_path / ".apm" / "skills"
        if not sub_skills_dir.is_dir():
            return 0

        parent_name = package_path.name
        github_skills_root = project_root / ".github" / "skills"
        github_skills_root.mkdir(parents=True, exist_ok=True)
        count = self._promote_sub_skills(
            sub_skills_dir, github_skills_root, parent_name, warn=True
        )

        # Also promote into .claude/skills/ when .claude/ exists
        claude_dir = project_root / ".claude"
        if claude_dir.exists() and claude_dir.is_dir():
            claude_skills_root = claude_dir / "skills"
            self._promote_sub_skills(
                sub_skills_dir, claude_skills_root, parent_name, warn=False
            )

        # Also promote into .opencode/skills/ when .opencode/ exists
        opencode_dir = project_root / ".opencode"
        if opencode_dir.exists() and opencode_dir.is_dir():
            opencode_skills_root = opencode_dir / "skills"
            self._promote_sub_skills(
                sub_skills_dir, opencode_skills_root, parent_name, warn=False
            )

        return count

    def _integrate_native_skill(
        self, package_info, project_root: Path, source_skill_md: Path
    ) -> SkillIntegrationResult:
        """Copy a native Skill (with existing SKILL.md) to .github/skills/ and optionally .claude/skills/.

        For packages that already have a SKILL.md at their root (like those from
        awesome-claude-skills), we copy the entire skill folder rather than
        regenerating from .apm/ primitives.

        The skill folder name is the source folder name (e.g., `mcp-builder`),
        validated and normalized per the agentskills.io spec.

        Source SKILL.md is copied verbatim — no metadata injection. Orphan
        detection uses apm.lock via directory name matching instead.

        T7 Enhancement: Also copies to .claude/skills/ when .claude/ folder exists.
        This ensures Claude Code users get skills while not polluting projects
        that don't use Claude.

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
        # e.g., apm_modules/ComposioHQ/awesome-claude-skills/mcp-builder → mcp-builder
        raw_skill_name = package_path.name

        # Validate skill name per agentskills.io spec
        is_valid, error_msg = validate_skill_name(raw_skill_name)
        if is_valid:
            skill_name = raw_skill_name
        else:
            # Normalize the name if validation fails
            skill_name = normalize_skill_name(raw_skill_name)
            # Log warning about name normalization (import here to avoid circular import)
            try:
                from apm_cli.cli import _rich_warning

                _rich_warning(
                    f"Skill name '{raw_skill_name}' normalized to '{skill_name}' ({error_msg})"
                )
            except ImportError:
                pass  # CLI not available in tests

        # Primary target: .github/skills/
        github_skill_dir = project_root / ".github" / "skills" / skill_name
        github_skill_md = github_skill_dir / "SKILL.md"

        # Always copy — source integrity is preserved, orphan detection uses apm.lock
        skill_created = not github_skill_dir.exists()
        skill_updated = not skill_created

        files_copied = 0
        claude_skill_dir: Path | None = None

        # === Copy to .github/skills/ (primary) ===
        if github_skill_dir.exists():
            shutil.rmtree(github_skill_dir)

        github_skill_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            package_path, github_skill_dir, ignore=shutil.ignore_patterns(".apm")
        )

        files_copied = sum(1 for _ in github_skill_dir.rglob("*") if _.is_file())

        # === Promote sub-skills to top-level entries ===
        # Packages may contain sub-skills in .apm/skills/*/ subdirectories.
        # Copilot only discovers .github/skills/<name>/SKILL.md (direct children),
        # so we promote each sub-skill to an independent top-level entry.
        sub_skills_dir = package_path / ".apm" / "skills"
        github_skills_root = project_root / ".github" / "skills"
        sub_skills_count = self._promote_sub_skills(
            sub_skills_dir, github_skills_root, skill_name, warn=True
        )

        # === T7: Copy to .claude/skills/ (secondary - compatibility) ===
        claude_dir = project_root / ".claude"
        if claude_dir.exists() and claude_dir.is_dir():
            claude_skill_dir = claude_dir / "skills" / skill_name

            if claude_skill_dir.exists():
                shutil.rmtree(claude_skill_dir)

            claude_skill_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                package_path, claude_skill_dir, ignore=shutil.ignore_patterns(".apm")
            )

            # Promote sub-skills for Claude too
            claude_skills_root = claude_dir / "skills"
            self._promote_sub_skills(
                sub_skills_dir, claude_skills_root, skill_name, warn=False
            )

        # === OpenCode compatibility: Copy to .opencode/skills/ (secondary) ===
        opencode_dir = project_root / ".opencode"
        if opencode_dir.exists() and opencode_dir.is_dir():
            opencode_skill_dir = opencode_dir / "skills" / skill_name

            if opencode_skill_dir.exists():
                shutil.rmtree(opencode_skill_dir)

            opencode_skill_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                package_path, opencode_skill_dir, ignore=shutil.ignore_patterns(".apm")
            )

            # Promote sub-skills for OpenCode too
            opencode_skills_root = opencode_dir / "skills"
            self._promote_sub_skills(
                sub_skills_dir, opencode_skills_root, skill_name, warn=False
            )

        return SkillIntegrationResult(
            skill_created=skill_created,
            skill_updated=skill_updated,
            skill_skipped=False,
            skill_path=github_skill_md,
            references_copied=files_copied,
            links_resolved=0,
            sub_skills_promoted=sub_skills_count,
        )

    def integrate_package_skill(
        self, package_info, project_root: Path
    ) -> SkillIntegrationResult:
        """Integrate a package's skill into .github/skills/ directory.

        Copies native skills (packages with SKILL.md at root) to .github/skills/
        and optionally .claude/skills/. Also promotes any sub-skills from .apm/skills/.

        Packages without SKILL.md at root are not installed as skills — only their
        sub-skills (if any) are promoted.

        Args:
            package_info: PackageInfo object with package metadata
            project_root: Root directory of the project

        Returns:
            SkillIntegrationResult: Results of the integration operation
        """
        # Check if package type allows skill installation (T4 routing)
        # SKILL and HYBRID → install as skill
        # INSTRUCTIONS and PROMPTS → skip skill installation
        if not should_install_skill(package_info):
            # Even non-skill packages may ship sub-skills under .apm/skills/.
            # Promote them so Copilot can discover them independently.
            sub_skills_count = self._promote_sub_skills_standalone(
                package_info, project_root
            )
            return SkillIntegrationResult(
                skill_created=False,
                skill_updated=False,
                skill_skipped=True,
                skill_path=None,
                references_copied=0,
                links_resolved=0,
                sub_skills_promoted=sub_skills_count,
            )

        # Skip virtual FILE and COLLECTION packages - they're individual files, not full packages
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
            return self._integrate_native_skill(
                package_info, project_root, source_skill_md
            )

        # No SKILL.md at root — not a skill package.
        # Still promote any sub-skills shipped under .apm/skills/.
        sub_skills_count = self._promote_sub_skills_standalone(
            package_info, project_root
        )
        return SkillIntegrationResult(
            skill_created=False,
            skill_updated=False,
            skill_skipped=True,
            skill_path=None,
            references_copied=0,
            links_resolved=0,
            sub_skills_promoted=sub_skills_count,
        )

    def sync_integration(self, apm_package, project_root: Path) -> Dict[str, int]:
        """Sync .github/skills/ and .claude/skills/ with currently installed packages.

        Removes skill directories for packages that are no longer installed.
        Uses npm-style approach: derives expected skill directory names from
        installed dependencies and removes any directory not in that set.

        T7 Enhancement: Cleans both .github/skills/ and .claude/skills/ locations.

        Args:
            apm_package: APMPackage with current dependencies
            project_root: Root directory of the project

        Returns:
            Dict with cleanup statistics
        """
        stats = {"files_removed": 0, "errors": 0}

        # Build set of expected skill directory names from installed packages
        installed_skill_names = set()
        for dep in apm_package.get_apm_dependencies():
            # Derive skill name the same way copy_native_skill / copy_skill_to_target does
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
                    if (
                        sub_skill_path.is_dir()
                        and (sub_skill_path / "SKILL.md").exists()
                    ):
                        raw_sub = sub_skill_path.name
                        is_valid, _ = validate_skill_name(raw_sub)
                        installed_skill_names.add(
                            raw_sub if is_valid else normalize_skill_name(raw_sub)
                        )

        # Clean .github/skills/ (primary)
        github_skills_dir = project_root / ".github" / "skills"
        if github_skills_dir.exists():
            result = self._clean_orphaned_skills(
                github_skills_dir, installed_skill_names
            )
            stats["files_removed"] += result["files_removed"]
            stats["errors"] += result["errors"]

        # Clean .claude/skills/ (secondary - T7 compatibility)
        claude_skills_dir = project_root / ".claude" / "skills"
        if claude_skills_dir.exists():
            result = self._clean_orphaned_skills(
                claude_skills_dir, installed_skill_names
            )
            stats["files_removed"] += result["files_removed"]
            stats["errors"] += result["errors"]

        # Clean .opencode/skills/ (OpenCode compatibility)
        opencode_skills_dir = project_root / ".opencode" / "skills"
        if opencode_skills_dir.exists():
            result = self._clean_orphaned_skills(
                opencode_skills_dir, installed_skill_names
            )
            stats["files_removed"] += result["files_removed"]
            stats["errors"] += result["errors"]

        return stats

    def _clean_orphaned_skills(
        self, skills_dir: Path, installed_skill_names: set
    ) -> Dict[str, int]:
        """Clean orphaned skills from a skills directory.

        Uses npm-style approach: any skill directory not matching an installed
        package name is considered orphaned and removed.

        Args:
            skills_dir: Path to skills directory (.github/skills/ or .claude/skills/)
            installed_skill_names: Set of expected skill directory names

        Returns:
            Dict with cleanup statistics
        """
        files_removed = 0
        errors = 0

        for skill_subdir in skills_dir.iterdir():
            if skill_subdir.is_dir():
                if skill_subdir.name not in installed_skill_names:
                    try:
                        shutil.rmtree(skill_subdir)
                        files_removed += 1
                    except Exception:
                        errors += 1

        return {"files_removed": files_removed, "errors": errors}

    def update_gitignore_for_skills(self, project_root: Path) -> bool:
        """Update .gitignore with pattern for integrated skills.

        Args:
            project_root: Root directory of the project

        Returns:
            bool: True if .gitignore was updated, False if pattern already exists
        """
        gitignore_path = project_root / ".gitignore"

        patterns = [
            ".github/skills/*-apm/",  # APM integrated skills use -apm suffix
            "# APM integrated skills",
        ]

        # Read current content
        current_content = []
        if gitignore_path.exists():
            try:
                with open(gitignore_path, "r", encoding="utf-8") as f:
                    current_content = [line.rstrip("\n\r") for line in f.readlines()]
            except Exception:
                return False

        # Check which patterns need to be added
        patterns_to_add = []
        for pattern in patterns:
            if not any(pattern in line for line in current_content):
                patterns_to_add.append(pattern)

        if not patterns_to_add:
            return False

        # Add patterns to .gitignore
        try:
            with open(gitignore_path, "a", encoding="utf-8") as f:
                if current_content and current_content[-1].strip():
                    f.write("\n")
                f.write("\n# APM integrated skills\n")
                for pattern in patterns_to_add:
                    f.write(f"{pattern}\n")
            return True
        except Exception:
            return False
