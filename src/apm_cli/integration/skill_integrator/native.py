# pylint: disable=duplicate-code
"""Skill integration functionality for APM packages (Claude Code & Cursor support)."""

from pathlib import Path

from ._native_helpers import _integrate_native_skill
from .class_ import SkillIntegrationResult
from .opts import SkillOpts, SkillPromoteOpts
from .typing_helpers import should_install_skill


def _integrate_skill_bundle(
    self,
    package_info,
    project_root: Path,
    skills_dir: Path,
    opts: SkillOpts | None = None,
) -> SkillIntegrationResult:
    """Promote every skill in a SKILL_BUNDLE's top-level skills/ directory.

    Reuses the same promotion logic as _promote_sub_skills but sources
    from package_root/skills/ instead of .apm/skills/.  Each nested
    skill directory becomes a top-level skill in every target.

    Args:
        package_info: PackageInfo with package metadata.
        project_root: Root directory of the project.
        skills_dir: The package's skills/ directory.
        opts: Optional :class:`SkillOpts` controlling diagnostics, force, etc.

    Returns:
        SkillIntegrationResult with all promoted skills.
    """
    _opts = opts or SkillOpts()
    diagnostics = _opts.diagnostics
    managed_files = _opts.managed_files
    force = _opts.force
    logger = _opts.logger
    targets = _opts.targets
    skill_subset = _opts.skill_subset
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
            SkillPromoteOpts(
                warn=is_primary,
                owned_by=owned_by if is_primary else None,
                diagnostics=diagnostics if is_primary else None,
                managed_files=managed_files if is_primary else None,
                force=force,
                project_root=project_root,
                logger=logger if is_primary else None,
                name_filter=_name_filter,
            ),
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
    opts: SkillOpts | None = None,
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
        opts: Optional :class:`SkillOpts` controlling diagnostics, force, etc.

    Returns:
        SkillIntegrationResult: Results of the integration operation
    """
    _opts = opts or SkillOpts()
    diagnostics = _opts.diagnostics
    managed_files = _opts.managed_files
    force = _opts.force
    logger = _opts.logger
    targets = _opts.targets
    skill_subset = _opts.skill_subset
    # Check if package type allows skill installation (T4 routing)
    # SKILL and HYBRID -> install as skill
    # INSTRUCTIONS and PROMPTS -> skip skill installation
    if not should_install_skill(package_info):
        # Even non-skill packages may ship sub-skills under .apm/skills/.
        # Promote them so Copilot can discover them independently.
        sub_skills_count, sub_deployed = self._promote_sub_skills_standalone(
            package_info,
            project_root,
            SkillOpts(
                diagnostics=diagnostics,
                managed_files=managed_files,
                force=force,
                logger=logger,
                targets=targets,
            ),
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
            opts,
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
            opts,
        )
    # No SKILL.md at root  -- not a skill package.
    # Still promote any sub-skills shipped under .apm/skills/.
    sub_skills_count, sub_deployed = self._promote_sub_skills_standalone(
        package_info,
        project_root,
        SkillOpts(
            diagnostics=diagnostics,
            managed_files=managed_files,
            force=force,
            logger=logger,
            targets=targets,
        ),
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
