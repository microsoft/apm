"""Skill integration functionality for APM packages (Claude Code & Cursor support)."""

import shutil
from pathlib import Path

from .naming import normalize_skill_name, validate_skill_name


def _build_skill_prefixes(source) -> list[str]:
    """Return the list of skill-path prefixes for *source* targets.

    Dynamic-root (cowork) targets contribute a ``cowork://`` URI prefix;
    regular targets contribute ``<effective_root>/skills/`` path prefixes.
    """
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
    return skill_prefixes


def _build_installed_skill_names(apm_package, project_root: Path) -> set[str]:
    """Return the set of expected skill directory names from installed packages.

    Derives names from top-level dependencies and their promoted sub-skills
    (via ``<install_path>/.apm/skills/``).
    """
    installed_skill_names: set[str] = set()
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
    return installed_skill_names


def _empty_sync_stats() -> dict[str, int]:
    """Return the default sync stats payload."""
    return {"files_removed": 0, "errors": 0}


def _resolve_sync_targets(targets):
    """Return explicit targets or the known target registry."""
    if targets is not None:
        return targets
    from apm_cli.integration.targets import KNOWN_TARGETS

    return list(KNOWN_TARGETS.values())


def _iter_managed_skill_paths(
    managed_files: set[str], skill_prefix_tuple: tuple[str, ...]
) -> list[str]:
    """Return managed skill entries under the active target prefixes."""
    return [
        rel_path
        for rel_path in managed_files
        if rel_path.startswith(skill_prefix_tuple) and ".." not in rel_path
    ]


def _ensure_cowork_root(
    cowork_root_resolved: bool,
    cowork_root_cached: Path | None,
) -> tuple[bool, Path | None]:
    """Resolve the cowork skills root at most once."""
    if cowork_root_resolved:
        return cowork_root_resolved, cowork_root_cached
    from apm_cli.integration.copilot_cowork_paths import resolve_copilot_cowork_skills_dir

    return True, resolve_copilot_cowork_skills_dir()


def _resolve_managed_skill_target(
    rel_path: str,
    project_root: Path,
    cowork_root: Path | None,
) -> tuple[Path | None, bool]:
    """Resolve a managed skill path to a filesystem target."""
    from apm_cli.integration.copilot_cowork_paths import COWORK_URI_SCHEME

    if rel_path.startswith(COWORK_URI_SCHEME):
        if cowork_root is None:
            return None, True
        from apm_cli.integration.copilot_cowork_paths import from_lockfile_path

        return from_lockfile_path(rel_path, cowork_root), False

    from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

    target = project_root / rel_path
    try:
        ensure_path_within(target, project_root)
    except PathTraversalError:
        return None, False
    return target, False


def _remove_sync_target(target: Path) -> None:
    """Remove a tracked file or directory."""
    if target.is_dir():
        shutil.rmtree(target)
        return
    target.unlink()


def _warn_skipped_cowork_entries(cowork_skipped: int) -> None:
    """Warn once when cowork cleanup is skipped for unresolved roots."""
    from apm_cli.utils.console import _rich_warning

    _rich_warning(
        f"Cowork: skipping {cowork_skipped} skill "
        f"{'entry' if cowork_skipped == 1 else 'entries'}"
        " -- OneDrive path not detected.\n"
        "Run: apm config set copilot-cowork-skills-dir <path>  "
        "(or set APM_COPILOT_COWORK_SKILLS_DIR)\n"
        "to clean up these entries on the next install/uninstall.",
        symbol="warning",
    )


def _cleanup_managed_skill_paths(
    project_root: Path,
    managed_files: set[str],
    skill_prefix_tuple: tuple[str, ...],
) -> dict[str, int]:
    """Remove tracked skill paths from managed targets only."""
    from apm_cli.integration.copilot_cowork_paths import COWORK_URI_SCHEME

    stats = _empty_sync_stats()
    cowork_root_resolved = False
    cowork_root_cached: Path | None = None
    cowork_skipped = 0

    for rel_path in _iter_managed_skill_paths(managed_files, skill_prefix_tuple):
        try:
            if rel_path.startswith(COWORK_URI_SCHEME):
                cowork_root_resolved, cowork_root_cached = _ensure_cowork_root(
                    cowork_root_resolved,
                    cowork_root_cached,
                )
            target, skipped = _resolve_managed_skill_target(
                rel_path,
                project_root,
                cowork_root_cached,
            )
        except Exception:
            stats["errors"] += 1
            continue
        if skipped:
            cowork_skipped += 1
            continue
        if target is None or not target.exists():
            continue
        try:
            _remove_sync_target(target)
            stats["files_removed"] += 1
        except Exception:
            stats["errors"] += 1

    if cowork_skipped > 0:
        _warn_skipped_cowork_entries(cowork_skipped)
    return stats


def _resolve_target_skills_dir(target, project_root: Path) -> Path | None:
    """Return the skills dir for *target*, if it should be cleaned."""
    skills_mapping = target.primitives["skills"]
    effective_root = skills_mapping.deploy_root or target.root_dir
    if skills_mapping.deploy_root and not (project_root / target.root_dir).is_dir():
        return None
    return project_root / effective_root / "skills"


def _cleanup_orphaned_target_skills(
    self,
    source,
    project_root: Path,
    installed_skill_names: set[str],
) -> dict[str, int]:
    """Run orphan cleanup across the active target skill directories."""
    stats = _empty_sync_stats()
    seen_cleanup_dirs: set[Path] = set()

    for target in source:
        if not target.supports("skills"):
            continue
        skills_dir = _resolve_target_skills_dir(target, project_root)
        if skills_dir is None:
            continue
        resolved_skills = skills_dir.resolve()
        if resolved_skills in seen_cleanup_dirs:
            import logging

            logging.getLogger(__name__).debug(
                "%s -- already processed, skipping cleanup for %s", skills_dir, target.name
            )
            continue
        seen_cleanup_dirs.add(resolved_skills)
        if not skills_dir.exists():
            continue
        result = self._clean_orphaned_skills(
            skills_dir,
            installed_skill_names,
            project_root=project_root,
        )
        stats["files_removed"] += result["files_removed"]
        stats["errors"] += result["errors"]

    return stats


def sync_integration(
    self,
    apm_package,
    project_root: Path,
    managed_files: set | None = None,  # noqa: RUF013
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
    source = _resolve_sync_targets(targets)
    skill_prefix_tuple = tuple(_build_skill_prefixes(source))

    if managed_files is not None:
        return _cleanup_managed_skill_paths(project_root, managed_files, skill_prefix_tuple)

    installed_skill_names = _build_installed_skill_names(apm_package, project_root)
    return _cleanup_orphaned_target_skills(
        self,
        source,
        project_root,
        installed_skill_names,
    )


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

        logging.getLogger(__name__).debug("Could not read lockfile for ownership check: %s", exc)
    return owned
