"""Skill integration functionality for APM packages (Claude Code & Cursor support)."""

import filecmp
import hashlib
import os
import re
import shutil
import stat
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from copy import copy
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from apm_cli.install.deployed_paths import deployed_path_entry
from apm_cli.install.services import enforce_agent_plugin_deployment_boundary
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.skill_package_routing import (
    get_effective_type,
)
from apm_cli.integration.skill_package_routing import (
    should_compile_instructions as _should_compile_instructions,
)
from apm_cli.integration.skill_package_routing import (
    should_install_skill as _should_install_skill,
)
from apm_cli.integration.skill_preflight import find_unmanaged_conflicts, selected_skill_sources
from apm_cli.integration.skill_support import (
    SkillIntegrationResult,
    build_copy_ignore,
    build_skill_ownership_maps,
    clean_orphaned_skills,
    get_lockfile_owned_agent_skills,
)
from apm_cli.integration.targets import TargetProfile
from apm_cli.models.dependency.subsets import skill_subset_filter_tokens
from apm_cli.utils.atomic_io import write_text_lf

__all__ = [
    "SkillIntegrationResult",
    "SkillIntegrator",
    "copy_skill_to_target",
    "get_effective_type",
    "normalize_skill_name",
    "should_compile_instructions",
    "should_install_skill",
    "to_hyphen_case",
    "validate_skill_name",
]

if TYPE_CHECKING:
    from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan
    from apm_cli.models.validation import PackageType


def should_install_skill(package_info) -> bool:
    """Return whether a package should be installed as a native skill."""
    return _should_install_skill(
        package_info,
        resolve_effective_type=get_effective_type,
    )


def should_compile_instructions(package_info) -> bool:
    """Return whether package instructions belong in compiled output."""
    return _should_compile_instructions(
        package_info,
        resolve_effective_type=get_effective_type,
    )


def _build_deployable_copy_ignore(
    *,
    skip_bin: bool = False,
    source_plan: "DeployableSourcePlan | None" = None,
    exclude_apm: bool = False,
    exclude_apm_yml: bool = False,
) -> Callable[[str, list[str]], list[str]]:
    """Build a copy filter constrained to the authorized source plan."""
    base_ignore = build_copy_ignore(skip_bin=skip_bin)
    internal_names = (
        *((".apm",) if exclude_apm else ()),
        *(("apm.yml",) if exclude_apm_yml else ()),
    )
    internal_filter = shutil.ignore_patterns(*internal_names) if internal_names else None

    def _combined(directory: str, contents: list[str]) -> list[str]:
        ignored = set(base_ignore(directory, contents))
        if internal_filter is not None:
            ignored.update(internal_filter(directory, contents))
        if source_plan is not None:
            ignored.update(source_plan.copy_ignore(directory, contents))
        return list(ignored)

    return _combined


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


def copy_skill_to_target(
    package_info,
    source_path: Path,
    target_base: Path,
    targets=None,
    *,
    source_plan: "DeployableSourcePlan | None" = None,
) -> list[Path]:
    """Copy admitted skill members to selected targets, preserving destination siblings.

    Direct callers without an authorized source plan receive a conservative
    skills-only plan. Missing targets fall back to ``active_targets()``.
    Return successful skill directories; rewrite outbound package links without
    injecting metadata into SKILL.md.
    """
    if not should_install_skill(package_info):
        return []
    if targets is None:
        from apm_cli.integration.targets import active_targets

        targets = active_targets(target_base)
    if source_plan is None:
        from types import SimpleNamespace

        from apm_cli.install.deployable_source_plan import DeployableSourcePlan

        plan_package = package_info
        if not isinstance(getattr(package_info, "install_path", None), Path):
            plan_package = SimpleNamespace(install_path=source_path)
        source_plan = DeployableSourcePlan.create(
            plan_package,
            targets,
            skill_subset=None,
            hooks_approved=False,
            canvas_approved=False,
            skip_bin=True,
        )
    source_path = source_path.resolve()
    if source_path != source_plan.source_root:
        raise ValueError("copy_skill_to_target source_path must match source_plan.source_root")

    from apm_cli.security.gate import BLOCK_POLICY

    verdict = source_plan.scan_security(policy=BLOCK_POLICY)
    if verdict.should_block:
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
    for target in targets:
        if not target.supports("skills"):
            continue
        skills_rel_root = target.skills_rel_root

        # Skip if target dir does not exist and auto_create is disabled
        target_root_dir = target_base / target.root_dir
        if not target.auto_create and not target_root_dir.is_dir():
            continue

        skill_dir = target_base / skills_rel_root / skill_name

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
        ensure_path_within(skill_dir, target_base / skills_rel_root)

        # Dedup: skip if same resolved path already deployed.
        resolved = skill_dir.resolve()
        if resolved in seen_skill_dirs:
            import logging

            logging.getLogger(__name__).debug(
                "%s -- already deployed, skipping for %s", skill_dir, target.name
            )
            continue
        seen_skill_dirs.add(resolved)

        rewriter = SkillIntegrator()
        rewriter.init_link_resolver(package_info, target_base)
        _, managed = rewriter._ownership_maps(target_base)
        copied = rewriter._copy_skill_bundle(
            source_path,
            skill_dir,
            target_base,
            ignore=_build_deployable_copy_ignore(source_plan=source_plan),
            managed_files=set(managed),
            force=True,
            diagnostics=None,
        )
        if copied:
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

    selected_skill_sources = staticmethod(selected_skill_sources)
    find_unmanaged_conflicts = find_unmanaged_conflicts

    def __init__(self) -> None:
        super().__init__()
        # Only successful writes establish same-run ownership, keyed by the
        # actual destination rather than a basename shared by several targets.
        self._native_skill_session_owners: dict[Path, str | None] = {}
        self._ownership_snapshots: (
            dict[Path, tuple[Mapping[str, str], Mapping[str, str]]] | None
        ) = None
        self._skill_files: list[Path] = []
        self._skill_recorded_hashes: dict[str, str] = {}
        self._skill_session_hashes: dict[str, str] = {}
        self._hash_snapshots: dict[Path, dict[str, str]] | None = None
        self._skill_targets: list[TargetProfile] | None = None
        self._replay_owners: Mapping[str, str] | None = None
        self._skill_current_owner: str | None = None
        self._skill_source_plan: DeployableSourcePlan | None = None
        # Any skipped member or unverified rewrite invalidates this call's completion proof.
        self._skill_can_complete = False
        self._skill_completed_roots: dict[Path, set[str]] = {}
        self._skill_completed_files: dict[Path, set[str]] = {}
        self._materialization_session: DeploymentMaterializationSession | None = None

    @contextmanager
    def ownership_snapshot(
        self,
        *,
        replay_lockfile=None,
        materialization_session: "DeploymentMaterializationSession | None" = None,
    ) -> Iterator[None]:
        """Reuse immutable durable ownership during one sequential deployment batch."""
        if self._ownership_snapshots is not None:
            raise RuntimeError("A skill ownership snapshot is already active")
        self._ownership_snapshots = {}
        self._hash_snapshots = {}
        self._materialization_session = materialization_session
        if replay_lockfile is not None:
            from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

            self._replay_owners = DeploymentLedgerCodec.active_file_owners(replay_lockfile)
        try:
            try:
                yield
            finally:
                if materialization_session is not None:
                    materialization_session.flush()
        finally:
            self._ownership_snapshots = None
            self._hash_snapshots = None
            self._skill_session_hashes.clear()
            self._replay_owners = None
            self._materialization_session = None

    def _skill_member_skipped(self, _path: Path) -> None:
        self._skill_can_complete = False

    def _skill_materialization_complete(self, project_root: Path) -> bool:
        """Require positive discovery and every admitted source at every physical target."""
        plan = self._skill_source_plan
        if (
            plan is None
            or plan.skill_discovery_complete is not True
            or not self._skill_can_complete
            or self._replay_owners is not None
        ):
            return False
        roots = {
            self._target_skills_root(target, project_root).resolve()
            for target in self._skill_targets or ()
            if target.supports("skills")
        }
        return bool(roots) and all(
            plan.skill_source_roots <= self._skill_completed_roots.get(root, set())
            and plan.skill_source_files <= self._skill_completed_files.get(root, set())
            for root in roots
        )

    def _skill_path_entry(self, path: Path, project_root: Path) -> str:
        """Use one canonical locator for collision checks and materialized members."""
        if self._skill_targets is None and not path.is_relative_to(project_root):
            return path.as_posix()
        return deployed_path_entry(path, project_root, self._skill_targets)

    def _copy_skill_bundle(
        self,
        source: Path,
        destination: Path,
        project_root: Path,
        *,
        ignore: Callable[[str, list[str]], list[str]],
        managed_files: set[str] | None,
        force: bool,
        diagnostics,
        unchanged: bool = False,
    ) -> list[Path]:
        """Record only successfully copied source members, never surviving sibling assets."""
        from collections import ChainMap

        from apm_cli.utils.content_hash import compute_file_hash

        def entry(path: Path) -> str:
            return self._skill_path_entry(path, project_root)

        hashes = ChainMap(
            self._skill_session_hashes if self._hash_snapshots is not None else {},
            self._skill_recorded_hashes,
        )
        directory_entry = entry(destination)
        anchor = entry(destination / "SKILL.md")
        written: list[Path] = []
        session = self._materialization_session
        owner = self._skill_current_owner
        targets = self._skill_targets or []
        if session is not None and (
            self._skill_source_plan is None or owner is None or not targets
        ):
            raise RuntimeError("Live skill materialization requires an admitted source plan.")
        copied = self.copy_directory_files(
            source,
            destination,
            ignore=ignore,
            path_entry=entry,
            is_managed=lambda path: (
                path in self._skill_session_hashes
                or (
                    managed_files is not None
                    and (
                        path in managed_files
                        or (path == anchor and directory_entry in managed_files)
                    )
                )
            ),
            recorded_hashes=hashes,
            baseline_hash=(
                (lambda path: session.baseline(path, targets)) if session is not None else None
            ),
            on_member_written=written.append,
            requires_write=(
                (lambda path: session.requires_target_write(path, targets, owner))
                if session is not None
                else None
            ),
            on_member_skipped=self._skill_member_skipped,
            unchanged=unchanged,
            write_allowed=lambda path: (
                self._replay_owners is None
                or self._replay_owners.get(path, self._skill_current_owner)
                == self._skill_current_owner
            ),
            force=force or managed_files is None,
            diagnostics=diagnostics,
        )
        if not unchanged:
            if self.link_resolver is None:
                self._skill_can_complete = False
            self._resolve_markdown_links_in_skill_bundle(source, destination, copied)
        written_hashes = {entry(path): compute_file_hash(path) for path in copied}
        if session is not None and written:
            if self.link_resolver is None:
                raise RuntimeError("Cannot record skill writes without final link rendering.")
            session.record({path: written_hashes[entry(path)] for path in written}, targets, owner)
        self._skill_session_hashes.update(written_hashes)
        self._skill_files.extend(copied)
        plan = self._skill_source_plan
        if plan is not None and plan.skill_discovery_complete is True:
            source_root = plan.source_root
            physical_root = destination.parent.resolve()
            self._skill_completed_roots.setdefault(physical_root, set()).add(
                source.relative_to(source_root).as_posix()
            )
            self._skill_completed_files.setdefault(physical_root, set()).update(
                (source / path.relative_to(destination)).relative_to(source_root).as_posix()
                for path in copied
            )
        return copied

    def _ownership_maps(self, lockfile_root: Path) -> tuple[Mapping[str, str], Mapping[str, str]]:
        """Read durable ownership once per used root in the current integration phase."""
        if self._ownership_snapshots is None:
            return self._build_ownership_maps(lockfile_root)
        root = lockfile_root.resolve()
        if root not in self._ownership_snapshots:
            owned_by, native_owners = self._build_ownership_maps(root)
            self._ownership_snapshots[root] = (
                MappingProxyType(owned_by),
                MappingProxyType(native_owners),
            )
        return self._ownership_snapshots[root]

    @staticmethod
    def _target_skills_root(target: TargetProfile, project_root: Path) -> Path:
        """Return the target skills root for static and dynamic-root targets."""
        return target.skills_deploy_path(project_root)

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
        copied_files: list[Path] | None = None,
    ) -> int:
        """Rewrite copied markdown; a vanished admitted member is not a successful copy."""
        links_resolved = 0
        for target_file in copied_files if copied_files is not None else target_root.rglob("*.md"):
            if target_file.suffix != ".md":
                continue
            source_file = source_root / target_file.relative_to(target_root)
            if any(not path.is_file() or path.is_symlink() for path in (source_file, target_file)):
                if copied_files is not None:
                    raise OSError(
                        f"Cannot finalize copied skill member {target_file}: source or destination "
                        "changed during deployment. Restore regular files and retry installation."
                    )
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
    def _skill_names_in_directory(skills_dir: Path) -> frozenset[str]:
        """Return deployable skill names from a directory that may be absent."""
        if not skills_dir.is_dir():
            return frozenset()
        try:
            return frozenset(
                child.name
                for child in skills_dir.iterdir()
                if child.is_dir() and (child / "SKILL.md").is_file()
            )
        except FileNotFoundError:
            return frozenset()

    @staticmethod
    def _skill_source_dir(package_path: Path) -> Path:
        """Return the single directory a package's skills are promoted from.

        A root ``skills/`` bundle wins whenever it holds at least one skill;
        otherwise the normalized ``.apm/skills/`` location supplies them.

        Private on purpose: a plugin's skills need not share one parent, so
        this answer is incomplete for ``MARKETPLACE_PLUGIN`` and reading it
        directly is how the raw tree overrode the declaration in the first
        place (#2537). ``skill_source_paths`` is the routing entry point --
        it consults this only for the types that do have a single source.
        """
        root_bundle = package_path / "skills"
        if SkillIntegrator._skill_names_in_directory(root_bundle):
            return root_bundle
        return package_path / ".apm" / "skills"

    @staticmethod
    def skill_source_paths(
        package_path: Path, package_type: "PackageType | None" = None
    ) -> dict[str, Path]:
        """Map every deployable skill name to the directory it is promoted from.

        Single source of truth for skill routing: deployment and ``--skill``
        enumeration both resolve through here, so the set a user may select
        can never drift from the set that actually deploys (issue #2530).

        For a ``MARKETPLACE_PLUGIN`` parser-owned normalized membership is
        authoritative. The parser records each normalized name's validated
        byte source, so this integrator never re-derives membership from the
        raw root ``skills/`` tree. An empty resolution is a real answer:
        ``"skills": []`` means no skills, and a missing or malformed receipt
        fails closed rather than falling back to staged package content.

        Callers must handle a root ``SKILL.md`` before asking: a native
        single-skill package deploys the bundle itself, not children.
        """
        from apm_cli.models.validation import PackageType

        if package_type is PackageType.MARKETPLACE_PLUGIN:
            from apm_cli.deps.plugin_parser import normalized_plugin_skill_sources

            resolved, _ = normalized_plugin_skill_sources(package_path)
            return resolved

        source = SkillIntegrator._skill_source_dir(package_path)
        return {name: source / name for name in SkillIntegrator._skill_names_in_directory(source)}

    @staticmethod
    def available_skill_names(package_info) -> frozenset[str] | None:
        """Return names selectable through ``--skill`` for one package."""
        enforce_agent_plugin_deployment_boundary(package_info)

        package_path = package_info.install_path
        if (package_path / "SKILL.md").is_file():
            return None

        source_paths = SkillIntegrator.skill_source_paths(package_path, package_info.package_type)
        from apm_cli.deps.plugin_parser import normalized_plugin_skill_sources

        plugin_source_paths, declared = normalized_plugin_skill_sources(package_path)
        if declared:
            source_paths = {**source_paths, **plugin_source_paths}
        available = set(source_paths)
        skills_root = package_path / "skills"
        for source_path in source_paths.values():
            if source_path.is_relative_to(skills_root):
                available.add(source_path.relative_to(skills_root).as_posix())
        return frozenset(available)

    @staticmethod
    def _skill_filter_misses_available(
        name_filter: set[str] | None,
        available_names: frozenset[str],
    ) -> bool:
        """Return whether a requested subset has no deployable source match."""
        return name_filter is not None and name_filter.isdisjoint(available_names)

    @staticmethod
    def _error_no_skill_filter_match(
        available_names: frozenset[str],
        requested_names: tuple[str, ...],
        parent_name: str,
        diagnostics=None,
        logger=None,
    ) -> None:
        """Fail an unsatisfied persisted required selection through the output cascade."""
        available_display = ", ".join(sorted(available_names)) if available_names else "(none)"
        requested_display = ", ".join(sorted(set(requested_names)))
        details = (
            "Skill selection matched no available skills. "
            f"Requested: {requested_display}. Available: {available_display}. "
            "Edit 'skills:' in apm.yml to use an available name or remove the filter, "
            "then run 'apm install'."
        )
        if diagnostics is not None:
            diagnostics.error(details, package=parent_name)
        elif logger:
            logger.error(f"Package '{parent_name}': {details}")
        else:
            try:
                from apm_cli.utils.console import _rich_error

                _rich_error(f"Package '{parent_name}': {details}", symbol="error")
            except ImportError:
                pass

    @staticmethod
    def _note_plugin_declaration_deploys_no_skills(
        package_path: Path,
        parent_name: str,
        diagnostics=None,
        logger=None,
    ) -> None:
        """Report a plugin whose declaration resolves to no skills at all.

        Zero deployable skills is a legitimate answer -- most plugins ship
        none. It stops being obvious once the package actually carries a
        root ``skills/`` bundle: those skills deployed before the declaration
        became authoritative (#2537), and now nothing does, with no line of
        output to tell "no skills by design" apart from "my declaration ate
        them". Say which names went unclaimed, once, at that exact shape.
        """
        from apm_cli.deps.plugin_parser import normalized_plugin_skill_sources

        _, declared = normalized_plugin_skill_sources(package_path)
        if not declared:
            return
        shadowed = SkillIntegrator._skill_names_in_directory(package_path / "skills")
        if not shadowed:
            return
        names = ", ".join(sorted(shadowed))
        message = (
            f"Package '{parent_name}': plugin.json declares no deployable skills, "
            f"so nothing under skills/ deploys ({names}). Declare the skill directories "
            "or their container in plugin.json, or remove the skills key to discover skills/."
        )
        detail = (
            "A 'skills' declaration replaces default discovery instead of adding "
            "to it. Declare each skill directory, or the container holding them, "
            "to deploy them -- or drop the 'skills' key to fall back to "
            "discovering skills/."
        )
        if diagnostics is not None:
            diagnostics.info(message, package=parent_name, detail=detail)
        elif logger:
            logger.info(message)
            logger.verbose_detail(detail)
        else:
            try:
                from apm_cli.utils.console import _rich_info

                _rich_info(message)
            except ImportError:
                pass

    @staticmethod
    def _promote_sub_skills(
        sub_skills_dir: Path,
        target_skills_root: Path,
        parent_name: str,
        *,
        warn: bool = True,
        skip_bin: bool = False,
        owned_by: Mapping[str, str] | None = None,
        diagnostics=None,
        managed_files=None,
        force: bool = False,
        project_root: Path | None = None,
        logger=None,
        name_filter: set[str] | None = None,
        link_rewriter: "SkillIntegrator | None" = None,
        source_plan: "DeployableSourcePlan | None" = None,
        source_paths: dict[str, Path] | None = None,
    ) -> tuple[int, list[Path]]:
        """Promote selected skills; return their count and successful anchor directories.

        Plugin ``source_paths`` resolve each declared skill independently, rather
        than assuming one parent. Declarations never authorize directory deletion.
        """
        promoted = 0
        deployed = []
        candidates: Iterable[Path]
        if source_paths is None:
            if not sub_skills_dir.is_dir():
                return promoted, deployed
            candidates = sub_skills_dir.iterdir()
        else:
            candidates = [source_paths[name] for name in sorted(source_paths)]

        materialization_root = project_root or target_skills_root.parent.parent

        for sub_skill_path in candidates:
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
            rewriter = link_rewriter or SkillIntegrator()
            rel_path = rewriter._skill_path_entry(target, materialization_root)
            target_existed = target.exists()
            unchanged = target_existed and SkillIntegrator.is_skill_dir_identical_to_source(
                sub_skill_path, target
            )
            is_self_overwrite = False
            if target_existed:
                # Check if this is a user-authored skill (not managed by APM)
                is_managed = (
                    managed_files is not None and rel_path.replace("\\", "/") in managed_files
                ) or (
                    rewriter._ownership_snapshots is not None
                    and target.resolve() in rewriter._native_skill_session_owners
                )
                prev_owner = (owned_by or {}).get(sub_name)
                is_self_overwrite = prev_owner is not None and prev_owner == parent_name

                if (
                    managed_files is not None
                    and not is_managed
                    and not is_self_overwrite
                    and not unchanged
                ):
                    # User-authored skill: respect force flag
                    if not force:
                        rewriter._skill_can_complete = False
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

            copied = rewriter._copy_skill_bundle(
                sub_skill_path,
                target,
                materialization_root,
                ignore=_build_deployable_copy_ignore(
                    skip_bin=skip_bin,
                    source_plan=source_plan,
                ),
                managed_files=managed_files,
                force=force,
                diagnostics=diagnostics,
                unchanged=unchanged,
            )
            if not copied:
                continue
            rewriter._native_skill_session_owners[target.resolve()] = parent_name
            promoted += 1
            if target / "SKILL.md" in copied:
                deployed.append(target)
                if warn and target_existed and not is_self_overwrite and not unchanged:
                    previous = f"'{prev_owner}'" if prev_owner else "another package"
                    detail = (
                        f"Skill '{sub_name}' from '{parent_name}' replaced overlapping files "
                        f"from {previous}; separately owned assets were preserved "
                        "-- remove one package to avoid this"
                    )
                    if diagnostics is not None:
                        diagnostics.overwrite(path=rel_path, package=parent_name, detail=detail)
                    elif logger:
                        logger.warning(detail)
                    else:
                        from apm_cli.utils.console import _rich_warning

                        _rich_warning(detail)
        return promoted, deployed

    _build_ownership_maps = staticmethod(build_skill_ownership_maps)

    @staticmethod
    def _build_skill_ownership_map(lockfile_root: Path) -> dict[str, str]:
        """Map skill names to durable owners for sub-skill collision detection."""
        owned_by, _ = SkillIntegrator._build_ownership_maps(lockfile_root)
        return owned_by

    @staticmethod
    def _build_native_skill_owner_map(lockfile_root: Path) -> dict[str, str]:
        """Project native destination ownership into the legacy skill-name view."""
        _, native_owners = SkillIntegrator._build_ownership_maps(lockfile_root)
        return {path.rsplit("/", 1)[-1]: owner for path, owner in native_owners.items()}

    def _promote_sub_skills_standalone(
        self,
        package_info,
        project_root: Path,
        *,
        lockfile_root: Path | None = None,
        diagnostics=None,
        managed_files=None,
        force: bool = False,
        logger=None,
        targets=None,
        skill_subset=None,
        skip_bin: bool = False,
        source_plan: "DeployableSourcePlan | None" = None,
        source_paths: dict[str, Path] | None = None,
    ) -> tuple[int, list[Path]]:
        """Promote an instruction package's .apm/skills without a parent skill entry.

        Respect explicit targets and skill subsets; return count and deployed paths.
        """
        self.init_link_resolver(package_info, project_root)
        package_path = package_info.install_path
        sub_skills_dir = package_path / ".apm" / "skills"
        name_filter = (
            source_plan.selected_skill_names
            if source_plan is not None
            else skill_subset_filter_tokens(skill_subset)
        )
        if not sub_skills_dir.is_dir() and name_filter is None:
            return 0, []

        if targets is None:
            from apm_cli.integration.targets import active_targets

            targets = active_targets(project_root)
        self._skill_targets = targets

        # Durable identity for self-overwrite comparison -- NOT package_path.name,
        # which is just the repo/leaf name and collides across owners (see
        # _build_ownership_maps).
        _dep_ref = getattr(package_info, "dependency_ref", None)
        parent_name = _dep_ref.get_unique_key() if _dep_ref is not None else package_path.name
        owned_by, _ = self._ownership_maps(lockfile_root or project_root)
        count = 0
        all_deployed: list[Path] = []
        seen_skill_dirs: set[Path] = set()
        primary_selected = False
        available_names = (
            frozenset(source_paths)
            if source_paths is not None
            else self._skill_names_in_directory(sub_skills_dir)
        )

        for target in targets:
            if not target.supports("skills"):
                continue

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
            is_primary = not primary_selected
            primary_selected = True

            target_skills_root.mkdir(parents=True, exist_ok=True)

            n, deployed = self._promote_sub_skills(
                sub_skills_dir,
                target_skills_root,
                parent_name,
                warn=is_primary,
                owned_by=owned_by if is_primary else None,
                diagnostics=diagnostics if is_primary else None,
                managed_files=managed_files,
                force=force,
                project_root=project_root,
                logger=logger if is_primary else None,
                name_filter=name_filter,
                link_rewriter=self,
                skip_bin=skip_bin,
                source_plan=source_plan,
                source_paths=source_paths,
            )
            if is_primary:
                count = n
            all_deployed.extend(deployed)

        if self._skill_filter_misses_available(name_filter, available_names):
            self._skill_can_complete = False
            self._error_no_skill_filter_match(
                available_names,
                tuple(skill_subset or ()),
                parent_name,
                diagnostics=diagnostics,
                logger=logger,
            )

        return count, all_deployed

    def _integrate_native_skill(
        self,
        package_info,
        project_root: Path,
        source_skill_md: Path,
        *,
        lockfile_root: Path | None = None,
        diagnostics=None,
        managed_files=None,
        force: bool = False,
        logger=None,
        targets=None,
        skip_bin: bool = False,
        source_plan: "DeployableSourcePlan | None" = None,
        source_paths: dict[str, Path] | None = None,
    ) -> SkillIntegrationResult:
        """Copy admitted native skill members, preserving separately owned destination files."""
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
        self._skill_targets = targets
        skill_created = False
        skill_updated = False
        files_copied = 0
        all_target_paths: list[Path] = []
        primary_skill_md: Path | None = None

        owned_by, lockfile_native_owners = self._ownership_maps(lockfile_root or project_root)
        # Install supplies the pre-normalized set; do not rescan it per package.
        collision_managed = (
            managed_files if managed_files is not None else set(lockfile_native_owners)
        )
        sub_skills_dir = package_path / ".apm" / "skills"

        # Full unique key of the package currently being installed.
        dep_ref = package_info.dependency_ref
        current_key: str | None = dep_ref.get_unique_key() if dep_ref is not None else None

        seen_skill_dirs: set[Path] = set()

        for target in targets:
            if not target.supports("skills"):
                continue

            is_primary = primary_skill_md is None  # first successful target owns result/diagnostics
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
                ensure_path_within(target_skill_dir, project_root / target.skills_rel_root)

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

            rel_path = deployed_path_entry(target_skill_dir, project_root, [target])
            session_owned = resolved in self._native_skill_session_owners
            if self.check_collision(
                target_skill_dir,
                rel_path,
                {rel_path} if session_owned else collision_managed,
                force,
                diagnostics=diagnostics,
            ):
                self._skill_can_complete = False
                continue

            target_existed = target_skill_dir.exists()
            from apm_cli.models.apm_package import PackageType as _PackageType

            copied = self._copy_skill_bundle(
                package_path,
                target_skill_dir,
                project_root,
                ignore=_build_deployable_copy_ignore(
                    skip_bin=skip_bin,
                    source_plan=source_plan,
                    exclude_apm=True,
                    exclude_apm_yml=package_info.package_type is _PackageType.MARKETPLACE_PLUGIN,
                ),
                managed_files=collision_managed,
                force=force,
                diagnostics=diagnostics,
            )
            if target_skill_dir / "SKILL.md" in copied:
                all_target_paths.append(target_skill_dir)
                if is_primary:
                    skill_created = not target_existed
                    skill_updated = target_existed
                    primary_skill_md = target_skill_dir / "SKILL.md"
                    files_copied = len(copied)
                if target_existed and is_primary:
                    # Check both the lockfile (previous runs) and the in-memory session
                    # map (current run) so that same-manifest collisions are caught even
                    # before the lockfile has been written for this run.
                    prev_owner = lockfile_native_owners.get(
                        rel_path
                    ) or self._native_skill_session_owners.get(resolved)
                    is_self_overwrite = prev_owner is not None and prev_owner == current_key
                    if prev_owner is not None and not is_self_overwrite:
                        try:
                            rel_prefix = target_skill_dir.parent.relative_to(
                                project_root
                            ).as_posix()
                        except ValueError:
                            # Dynamic-root targets (cowork): directory is
                            # outside the project tree.
                            rel_prefix = target.skills_subdir
                        rel_path = f"{rel_prefix}/{skill_name}"
                        # Issue 1: package= should identify the package causing the
                        # collision (current_key), not the skill name, so render_summary()
                        # groups diagnostics by the package responsible.
                        # Issue 2: message must tell the user what to do ("So What?" test).
                        detail = (
                            f"Skill '{skill_name}' from '{current_key}' replaced overlapping files "
                            f"from '{prev_owner}'; separately owned assets were preserved "
                            "-- remove one package to avoid this"
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
                self._native_skill_session_owners[resolved] = current_key

            # Promote sub-skills for this target. Identify the parent by its
            # durable unique key (current_key), not skill_name -- the folder
            # name collides across owners (see _build_ownership_maps).
            target_skills_root = self._target_skills_root(target, project_root)
            _, sub_deployed = self._promote_sub_skills(
                sub_skills_dir,
                target_skills_root,
                current_key or skill_name,
                warn=is_primary,
                owned_by=owned_by if is_primary else None,
                diagnostics=diagnostics if is_primary else None,
                managed_files=managed_files,
                force=force,
                project_root=project_root,
                logger=logger if is_primary else None,
                link_rewriter=self,
                skip_bin=skip_bin,
                source_plan=source_plan,
                source_paths=source_paths,
            )
            all_target_paths.extend(sub_deployed)

        # Count unique sub-skills from primary target only
        primary_root = project_root / ".github" / "skills"
        sub_skills_count = sum(
            1 for p in all_target_paths if p.parent == primary_root and p.name != skill_name
        )

        return SkillIntegrationResult(
            skill_created=skill_created,
            skill_updated=skill_updated,
            skill_skipped=not self._skill_files,
            skill_path=primary_skill_md,
            references_copied=files_copied,
            links_resolved=0,
            sub_skills_promoted=sub_skills_count,
            target_paths=all_target_paths,
            deployed_files=tuple(self._skill_files),
            complete=self._skill_materialization_complete(project_root),
        )

    def _integrate_skill_bundle(
        self,
        package_info,
        project_root: Path,
        skills_dir: Path,
        *,
        lockfile_root: Path | None = None,
        diagnostics=None,
        managed_files=None,
        force: bool = False,
        logger=None,
        targets=None,
        skill_subset=None,
        skip_bin: bool = False,
        source_plan: "DeployableSourcePlan | None" = None,
        source_paths: dict[str, Path] | None = None,
    ) -> SkillIntegrationResult:
        """Promote admitted bundle skills using the shared file-level materializer."""
        self.init_link_resolver(package_info, project_root)
        if targets is None:
            from apm_cli.integration.targets import active_targets

            targets = active_targets(project_root)
        self._skill_targets = targets

        # Durable identity for self-overwrite comparison -- NOT install_path.name,
        # which is just the repo/leaf name and collides across owners (see
        # _build_ownership_maps).
        _dep_ref = getattr(package_info, "dependency_ref", None)
        parent_name = (
            _dep_ref.get_unique_key() if _dep_ref is not None else package_info.install_path.name
        )
        owned_by, _ = self._ownership_maps(lockfile_root or project_root)

        total_promoted = 0
        all_deployed: list[Path] = []
        any_created = False
        seen_skill_dirs: set[Path] = set()
        primary_selected = False
        available_names = (
            frozenset(source_paths)
            if source_paths is not None
            else self._skill_names_in_directory(skills_dir)
        )

        # Convert skill_subset tuple to promotion filter tokens for O(1) lookup.
        _name_filter = (
            source_plan.selected_skill_names
            if source_plan is not None
            else skill_subset_filter_tokens(skill_subset)
        )

        for target in targets:
            if not target.supports("skills"):
                continue

            target_skills_root = project_root / target.skills_rel_root

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
            is_primary = not primary_selected
            primary_selected = True

            target_skills_root.mkdir(parents=True, exist_ok=True)

            n, deployed = self._promote_sub_skills(
                skills_dir,
                target_skills_root,
                parent_name,
                warn=is_primary,
                owned_by=owned_by if is_primary else None,
                diagnostics=diagnostics if is_primary else None,
                managed_files=managed_files,
                force=force,
                project_root=project_root,
                logger=logger if is_primary else None,
                name_filter=_name_filter,
                link_rewriter=self,
                skip_bin=skip_bin,
                source_plan=source_plan,
                source_paths=source_paths,
            )
            if is_primary:
                total_promoted = n
                if n > 0:
                    any_created = True
            all_deployed.extend(deployed)

        if self._skill_filter_misses_available(_name_filter, available_names):
            self._skill_can_complete = False
            self._error_no_skill_filter_match(
                available_names,
                tuple(skill_subset or ()),
                parent_name,
                diagnostics=diagnostics,
                logger=logger,
            )

        return SkillIntegrationResult(
            skill_created=any_created,
            skill_updated=False,
            skill_skipped=False,
            skill_path=None,
            references_copied=0,
            links_resolved=0,
            sub_skills_promoted=total_promoted,
            target_paths=all_deployed,
            deployed_files=tuple(self._skill_files),
            complete=self._skill_materialization_complete(project_root),
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
        source_plan=None,
    ) -> SkillIntegrationResult:
        """Deploy admitted native skills, bundles and promoted sub-skills.

        Explicit targets and subsets narrow deployment; absent targets use
        normal discovery. Executable consent is supplied separately through
        ``skip_bin`` and ``trust_bin``. The result distinguishes skill directories
        from successfully materialized files so shared destination contents
        never become a package's ownership proof.
        """
        enforce_agent_plugin_deployment_boundary(package_info)
        from apm_cli.core.scope import InstallScope, get_apm_dir

        lockfile_root = (
            get_apm_dir(InstallScope.USER) if scope is InstallScope.USER else project_root
        )
        from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        self._skill_files = []
        self._skill_targets = targets
        self._skill_source_plan = source_plan
        self._skill_can_complete = True
        self._skill_completed_roots = {}
        self._skill_completed_files = {}
        self._skill_current_owner = (
            package_info.dependency_ref.get_unique_key()
            if package_info.dependency_ref is not None
            else "."
        )
        snapshots = self._hash_snapshots if self._hash_snapshots is not None else {}
        if lockfile_root not in snapshots:
            lockfile = LockFile.read(get_lockfile_path(lockfile_root))
            snapshots[lockfile_root] = (
                DeploymentLedgerCodec.deployed_file_hashes(lockfile) if lockfile else {}
            )
        self._skill_recorded_hashes = snapshots[lockfile_root]

        # Canonicalize only the root; preserve descendant links and caller metadata.
        package_info = copy(package_info)
        package_info.install_path = package_info.install_path.resolve()
        selection = self.selected_skill_sources(package_info)

        if selection.route == "subskills":
            # Non-skill packages may still ship sub-skills under .apm/skills/.
            sub_skills_count, sub_deployed = self._promote_sub_skills_standalone(
                package_info,
                project_root,
                lockfile_root=lockfile_root,
                diagnostics=diagnostics,
                managed_files=managed_files,
                force=force,
                logger=logger,
                targets=targets,
                skill_subset=skill_subset,
                skip_bin=skip_bin,
                source_plan=source_plan,
                source_paths=selection.children,
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
                deployed_files=tuple(self._skill_files),
                complete=self._skill_materialization_complete(project_root),
            )

        if selection.route == "none":
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

        if package_info.package_type is _PackageType.MARKETPLACE_PLUGIN:
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
                    skip_bin=skip_bin,
                    source_plan=source_plan,
                )

        # Check if this is a native Skill (already has SKILL.md at root)
        source_skill_md = package_path / "SKILL.md"
        if selection.route == "native":
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
                    lockfile_root=lockfile_root,
                    diagnostics=diagnostics,
                    managed_files=managed_files,
                    force=force,
                    logger=logger,
                    targets=targets,
                    skip_bin=skip_bin,
                    source_plan=source_plan,
                    source_paths=selection.children,
                ),
                bin_paths,
                bin_skip_reason,
            )

        # The shared selector retains normalized plugin declarations, including
        # empty declarations, rather than re-discovering a raw skills tree.
        root_skills_dir = package_path / "skills"
        _source_paths = selection.children
        _is_plugin = package_info.package_type == _PackageType.MARKETPLACE_PLUGIN
        if _is_plugin and not _source_paths and not skill_subset_filter_tokens(skill_subset):
            self._note_plugin_declaration_deploys_no_skills(
                package_path,
                package_path.name,
                diagnostics=diagnostics,
                logger=logger,
            )
            return self._merge_bin_paths(
                SkillIntegrationResult(
                    skill_created=False,
                    skill_updated=False,
                    skill_skipped=True,
                    skill_path=None,
                    references_copied=0,
                    links_resolved=0,
                ),
                bin_paths,
                bin_skip_reason,
            )
        return self._merge_bin_paths(
            self._integrate_skill_bundle(
                package_info,
                project_root,
                package_path / ".apm" / "skills" if _is_plugin else root_skills_dir,
                lockfile_root=lockfile_root,
                source_paths=_source_paths,
                diagnostics=diagnostics,
                managed_files=managed_files,
                force=force,
                logger=logger,
                targets=targets,
                skill_subset=skill_subset,
                skip_bin=skip_bin,
                source_plan=source_plan,
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
        """Return a new result including plugin bins without claiming a skill was created.

        Bin writes clear ``skill_skipped`` and retain deployment paths; a refusal
        retains its reason. Neither supplies complete skill-materialization proof.
        """
        if not bin_paths and skip_reason is None:
            return result
        updates: dict = {}
        if bin_paths:
            updates["complete"] = False
            updates["bin_deployed"] = len(bin_paths)
            updates["skill_skipped"] = False
            updates["target_paths"] = (result.target_paths or []) + bin_paths
            updates["deployed_files"] = (*result.deployed_files, *bin_paths)
        if skip_reason is not None:
            updates["complete"] = False
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
        skip_bin: bool = True,
        source_plan=None,
    ) -> tuple[list[Path], str | None]:
        """Deploy authorized legacy-plugin bin/ and manifest to user-scope Claude.

        Claude loads ``.claude-plugin/plugin.json`` as ``<name>@skills-dir``
        and exposes bin/ on PATH. POSIX executables receive user-only permissions.
        Warn unless explicit ``trust_bin=True`` consent was supplied; refusal
        arrives through ``skip_bin``. Return paths plus an actionable skip reason
        for project scope or missing Claude target, not for absent/denied binaries.
        """
        from apm_cli.core.scope import InstallScope
        from apm_cli.utils.path_security import validate_path_segments

        bin_dir = package_info.install_path / "bin"
        if not bin_dir.is_dir():
            return [], None

        # The package ships executables -- from here a non-deploy is a
        # reportable skip, not a silent no-op.
        if targets is None:
            from apm_cli.integration.targets import active_targets

            targets = active_targets(project_root)
        deployable = getattr(source_plan, "plugin_bin_deployable", None)
        if deployable is None:
            from apm_cli.install.exec_gate import plugin_bin_deployable

            deployable = plugin_bin_deployable(
                package_info,
                targets,
                project_root=project_root,
                scope=scope,
                policy=policy,
                skip_bin=skip_bin,
            )
        if not deployable:
            if scope is InstallScope.PROJECT:
                if logger:
                    logger.progress(
                        "bin/ deploy is user-scope only; skipping for project-scope install",
                        symbol="info",
                    )
                return [], "project_scope"
            if not any(t.name == "claude" and t.supports("skills") for t in targets):
                if logger:
                    logger.progress(
                        "bin/ present but no active Claude skills target; skipping bin deploy for "
                        f"{package_info.get_canonical_dependency_string()}",
                        symbol="warning",
                    )
                return [], "no_claude_target"
            return [], None

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
            target_root_dir = project_root / target.root_dir
            if not target.auto_create and not target_root_dir.is_dir():
                continue

            skill_base = project_root / target.skills_rel_root / skill_name
            rel_prefix = f"{target.skills_rel_root}/{skill_name}"
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
            logger.warning(
                f"Plugin '{canonical}' adds executables to Claude Code's PATH. "
                "Pass --trust-bin to allow, or --no-trust-bin to skip bin/ deployment.",
            )

        return deployed, None

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
        removed_paths: list[Path] | None = None,
    ) -> dict[str, int]:
        """Sync recorded skill paths, or use legacy package-name orphan detection.

        Scope-resolved ``targets`` supply skill prefixes; absent targets use the
        catalog. An explicit managed set limits cleanup to those recorded paths.
        Return removed-file and error counts.
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
            skill_prefixes.append(f"{t.skills_rel_root}/")
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
                    if removed_paths is not None:
                        removed_paths.append(target)
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

            # Special guard for cross-tool deploy_root (.agents/)
            # Only clean if the owning target dir exists
            if sm.deploy_root:
                if not (project_root / t.root_dir).is_dir():
                    continue

            skills_dir = project_root / t.skills_rel_root

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
                    skills_dir,
                    installed_skill_names,
                    project_root=project_root,
                    removed_paths=removed_paths,
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
        removed_paths: list[Path] | None = None,
    ) -> dict[str, int]:
        """Clean legacy-orphan skill directories without touching foreign agents."""
        return clean_orphaned_skills(
            skills_dir,
            installed_skill_names,
            project_root=project_root,
            get_lockfile_owned_agent_skills=self._get_lockfile_owned_agent_skills,
            removed_paths=removed_paths,
        )

    @staticmethod
    def _get_lockfile_owned_agent_skills(project_root: Path) -> set[str]:
        """Return the lockfile-owned ``.agents/skills`` names."""
        return get_lockfile_owned_agent_skills(project_root)
