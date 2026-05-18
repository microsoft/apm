"""Native skill integration helpers extracted from native.py."""

from __future__ import annotations

import shutil
from pathlib import Path

from .class_ import SkillIntegrationResult
from .naming import normalize_skill_name, validate_skill_name
from .opts import SkillCollisionOpts, SkillOpts, SkillPromoteOpts


def _get_normalized_skill_name(raw_name: str, diagnostics, logger) -> str:
    """Validate *raw_name* per agentskills.io spec; normalize and warn if invalid.

    Returns the final skill name (validated as-is, or normalized).
    """
    is_valid, error_msg = validate_skill_name(raw_name)
    if is_valid:
        return raw_name
    skill_name = normalize_skill_name(raw_name)
    if diagnostics is not None:
        diagnostics.warn(
            f"Skill name '{raw_name}' normalized to '{skill_name}' ({error_msg})",
            package=raw_name,
        )
    elif logger:
        logger.warning(f"Skill name '{raw_name}' normalized to '{skill_name}' ({error_msg})")
    else:
        try:
            from apm_cli.utils.console import _rich_warning

            _rich_warning(f"Skill name '{raw_name}' normalized to '{skill_name}' ({error_msg})")
        except ImportError:
            pass  # CLI not available in tests
    return skill_name


def _warn_skill_collision(
    self,
    skill_name: str,
    target_skill_dir: Path,
    project_root: Path,
    opts: SkillCollisionOpts | None = None,
) -> None:
    """Emit a collision warning when a skill dir is about to be overwritten.

    Checks both the lockfile (previous runs) and the in-memory session map
    (current run via ``self._native_skill_session_owners``) so same-manifest
    collisions are caught before the lockfile has been written for this run.
    """
    resolved_opts = opts or SkillCollisionOpts()
    prev_owner = (resolved_opts.lockfile_native_owners or {}).get(
        skill_name
    ) or self._native_skill_session_owners.get(skill_name)
    is_self_overwrite = prev_owner is not None and prev_owner == resolved_opts.current_key
    if prev_owner is None or is_self_overwrite:
        return
    try:
        rel_prefix = target_skill_dir.parent.relative_to(project_root).as_posix()
    except ValueError:
        rel_prefix = "skills"
    rel_path = f"{rel_prefix}/{skill_name}"
    detail = (
        f"Skill '{skill_name}' from '{resolved_opts.current_key}' replaced "
        f"'{prev_owner}' -- remove one package to avoid this"
    )
    if resolved_opts.diagnostics is not None:
        resolved_opts.diagnostics.overwrite(
            path=rel_path,
            package=resolved_opts.current_key or skill_name,
            detail=detail,
        )
        return
    if resolved_opts.logger:
        resolved_opts.logger.warning(detail)
        return
    from apm_cli.utils.console import _rich_warning

    _rich_warning(detail)


def _resolve_skill_targets(project_root: Path, targets):
    """Return explicit targets or discover active ones for *project_root*."""
    if targets is not None:
        return targets
    from apm_cli.integration.targets import active_targets

    return active_targets(project_root)


def _resolve_target_skill_paths(target, project_root: Path, skill_name: str) -> tuple[Path, Path]:
    """Return ``(skills_root, skill_dir)`` for *target*."""
    skills_mapping = target.primitives["skills"]
    if target.resolved_deploy_root is not None:
        target_skills_root = target.resolved_deploy_root
    else:
        effective_root = skills_mapping.deploy_root or target.root_dir
        target_skills_root = project_root / effective_root / "skills"
    return target_skills_root, target_skills_root / skill_name


def _validate_target_skill_dir(
    skill_name: str, target_skill_dir: Path, target_skills_root: Path
) -> None:
    """Validate the target skill path before copying into it."""
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
    ensure_path_within(target_skill_dir, target_skills_root)


def _ignore_non_content_and_apm(directory: str, contents: list[str]) -> list[str]:
    """Ignore non-content files plus the package-private ``.apm`` dir."""
    from apm_cli.security.gate import ignore_non_content

    apm_filter = shutil.ignore_patterns(".apm")
    return list(set(ignore_non_content(directory, contents)) | set(apm_filter(directory, contents)))


def _prepare_native_target_dir(
    self,
    target_skill_dir: Path,
    project_root: Path,
    collision_opts: SkillCollisionOpts,
    *,
    warn_on_overwrite: bool,
) -> None:
    """Warn about collisions and remove an existing target dir when needed."""
    if not target_skill_dir.exists():
        return
    if warn_on_overwrite:
        _warn_skill_collision(
            self,
            target_skill_dir.name,
            target_skill_dir,
            project_root,
            collision_opts,
        )
    shutil.rmtree(target_skill_dir)


def _promote_native_target_sub_skills(
    self,
    sub_skills_dir: Path,
    target_skills_root: Path,
    skill_name: str,
    promote_opts: SkillPromoteOpts,
) -> list[Path]:
    """Promote package sub-skills for a single target."""
    _, sub_deployed = self._promote_sub_skills(
        sub_skills_dir,
        target_skills_root,
        skill_name,
        promote_opts,
    )
    return sub_deployed


def _count_skill_files(target_skill_dir: Path) -> int:
    """Count deployed files within a target skill directory."""
    return sum(1 for path in target_skill_dir.rglob("*") if path.is_file())


def _integrate_native_skill(
    self,
    package_info,
    project_root: Path,
    source_skill_md: Path,
    opts: SkillOpts | None = None,
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
    package_path = package_info.install_path

    # Use the source folder name as the skill name
    # e.g., apm_modules/ComposioHQ/awesome-claude-skills/mcp-builder -> mcp-builder
    raw_skill_name = package_path.name

    # Validate skill name per agentskills.io spec; normalize if needed.
    skill_name = _get_normalized_skill_name(raw_skill_name, diagnostics, logger)

    targets = _resolve_skill_targets(project_root, targets)
    skill_created = False
    skill_updated = False
    files_copied = 0
    all_target_paths: list[Path] = []
    primary_skill_md: Path | None = None

    owned_by, lockfile_native_owners = self._build_ownership_maps(project_root)
    sub_skills_dir = package_path / ".apm" / "skills"
    dep_ref = package_info.dependency_ref
    current_key: str | None = dep_ref.get_unique_key() if dep_ref is not None else None
    collision_opts = SkillCollisionOpts(
        current_key=current_key,
        lockfile_native_owners=lockfile_native_owners,
        diagnostics=diagnostics,
        logger=logger,
    )
    seen_skill_dirs: set[Path] = set()

    for idx, target in enumerate(targets):
        if not target.supports("skills"):
            continue

        is_primary = idx == 0
        target_skills_root, target_skill_dir = _resolve_target_skill_paths(
            target,
            project_root,
            skill_name,
        )
        _validate_target_skill_dir(skill_name, target_skill_dir, target_skills_root)

        resolved_target = target_skill_dir.resolve()
        if resolved_target in seen_skill_dirs:
            if logger:
                logger.progress(
                    f"{target_skill_dir} -- already deployed, skipping for {target.name}",
                    symbol="info",
                )
            continue
        seen_skill_dirs.add(resolved_target)

        if is_primary:
            skill_created = not target_skill_dir.exists()
            skill_updated = not skill_created
            primary_skill_md = target_skill_dir / "SKILL.md"

        _prepare_native_target_dir(
            self,
            target_skill_dir,
            project_root,
            collision_opts,
            warn_on_overwrite=is_primary,
        )
        target_skill_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(package_path, target_skill_dir, ignore=_ignore_non_content_and_apm)
        all_target_paths.append(target_skill_dir)

        if is_primary:
            files_copied = _count_skill_files(target_skill_dir)

        all_target_paths.extend(
            _promote_native_target_sub_skills(
                self,
                sub_skills_dir,
                target_skills_root,
                skill_name,
                SkillPromoteOpts(
                    warn=is_primary,
                    owned_by=owned_by if is_primary else None,
                    diagnostics=diagnostics if is_primary else None,
                    managed_files=managed_files if is_primary else None,
                    force=force,
                    project_root=project_root,
                    logger=logger if is_primary else None,
                ),
            )
        )

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
