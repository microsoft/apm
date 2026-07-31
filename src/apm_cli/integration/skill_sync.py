"""Skill sync and cleanup helpers."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .skill_naming import normalize_skill_name

_log = logging.getLogger("apm_cli.integration.skill_integrator")


def _validate_skill_name(name: str) -> tuple[bool, str]:
    """Resolve the public validator lazily to avoid circular imports."""
    from .skill_integrator import validate_skill_name

    return validate_skill_name(name)


def _build_skill_prefixes(source: Any) -> tuple[str, ...]:
    """Build the valid lockfile prefixes for skill targets."""
    skill_prefixes: list[str] = []
    for target in source:
        if not target.supports("skills"):
            continue
        if target.user_root_resolver is not None:
            from apm_cli.integration.copilot_cowork_paths import COWORK_LOCKFILE_PREFIX

            if COWORK_LOCKFILE_PREFIX not in skill_prefixes:
                skill_prefixes.append(COWORK_LOCKFILE_PREFIX)
            continue
        skills_mapping = target.primitives["skills"]
        effective_root = skills_mapping.deploy_root or target.root_dir
        skill_prefixes.append(f"{effective_root}/skills/")
    return tuple(skill_prefixes)


def _resolve_managed_skill_target(
    rel_path: str,
    project_root: Path,
    project_root_resolved: Path,
    cowork_state: dict[str, Any],
) -> Path | None:
    """Resolve a managed skill lockfile path to a filesystem path."""
    from apm_cli.integration.copilot_cowork_paths import COWORK_URI_SCHEME

    if rel_path.startswith(COWORK_URI_SCHEME):
        if not cowork_state["resolved"]:
            from apm_cli.integration.copilot_cowork_paths import resolve_copilot_cowork_skills_dir

            cowork_state["root"] = resolve_copilot_cowork_skills_dir()
            cowork_state["resolved"] = True
        if cowork_state["root"] is None:
            cowork_state["skipped"] += 1
            return None
        from apm_cli.integration.copilot_cowork_paths import from_lockfile_path

        return from_lockfile_path(rel_path, cowork_state["root"])

    target = project_root / rel_path
    if not str(target.resolve()).startswith(str(project_root_resolved)):
        return None
    return target


def _sync_skills_managed_files(
    managed_files: set[str],
    project_root: Path,
    skill_prefix_tuple: tuple[str, ...],
    stats: dict[str, int],
    source: Any,
) -> None:
    """Remove managed skill paths from the deployment manifest."""
    project_root_resolved = project_root.resolve()
    cowork_state: dict[str, Any] = {"resolved": False, "root": None, "skipped": 0}
    for rel_path in managed_files:
        if not rel_path.startswith(skill_prefix_tuple) or ".." in rel_path:
            continue
        try:
            target = _resolve_managed_skill_target(
                rel_path, project_root, project_root_resolved, cowork_state
            )
        except Exception:
            stats["errors"] += 1
            continue
        if target is None or not target.exists():
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            stats["files_removed"] += 1
        except Exception:
            stats["errors"] += 1

    if cowork_state["skipped"] > 0:
        from apm_cli.utils.console import _rich_warning

        _rich_warning(
            f"Cowork: skipping {cowork_state['skipped']} skill "
            f"{'entry' if cowork_state['skipped'] == 1 else 'entries'}"
            " -- OneDrive path not detected.\n"
            "Run: apm config set copilot-cowork-skills-dir <path>  "
            "(or set APM_COPILOT_COWORK_SKILLS_DIR)\n"
            "to clean up these entries on the next install/uninstall.",
            symbol="warning",
        )


def _installed_skill_names(apm_package: Any, project_root: Path) -> set[str]:
    """Build expected skill directory names from installed packages."""
    installed_skill_names: set[str] = set()
    for dep in apm_package.get_apm_dependencies():
        raw_name = dep.repo_url.split("/")[-1]
        if dep.is_virtual and dep.virtual_path:
            raw_name = dep.virtual_path.split("/")[-1]
        is_valid, _ = _validate_skill_name(raw_name)
        skill_name = raw_name if is_valid else normalize_skill_name(raw_name)
        installed_skill_names.add(skill_name)

        install_path = dep.get_install_path(project_root / "apm_modules")
        sub_skills_dir = install_path / ".apm" / "skills"
        if sub_skills_dir.is_dir():
            for sub_skill_path in sub_skills_dir.iterdir():
                if sub_skill_path.is_dir() and (sub_skill_path / "SKILL.md").exists():
                    raw_sub = sub_skill_path.name
                    is_valid, _ = _validate_skill_name(raw_sub)
                    installed_skill_names.add(
                        raw_sub if is_valid else normalize_skill_name(raw_sub)
                    )
    return installed_skill_names


def _sync_skills_legacy(
    apm_package: Any,
    project_root: Path,
    source: Any,
    stats: dict[str, int],
    clean_fn: Callable[..., dict[str, int]],
) -> None:
    """Run legacy npm-style orphan detection for skills."""
    installed_skill_names = _installed_skill_names(apm_package, project_root)
    seen_cleanup_dirs: set[Path] = set()
    for target in source:
        if not target.supports("skills"):
            continue
        skills_mapping = target.primitives["skills"]
        effective_root = skills_mapping.deploy_root or target.root_dir
        if skills_mapping.deploy_root and not (project_root / target.root_dir).is_dir():
            continue

        skills_dir = project_root / effective_root / "skills"
        resolved_skills = skills_dir.resolve()
        if resolved_skills in seen_cleanup_dirs:
            _log.debug("%s -- already processed, skipping cleanup for %s", skills_dir, target.name)
            continue
        seen_cleanup_dirs.add(resolved_skills)

        if skills_dir.exists():
            result = clean_fn(skills_dir, installed_skill_names, project_root=project_root)
            stats["files_removed"] += result["files_removed"]
            stats["errors"] += result["errors"]


def sync_integration(
    link_rewriter: Any,
    apm_package: Any,
    project_root: Path,
    managed_files: set[str] | None = None,
    targets: Any = None,
) -> dict[str, int]:
    """Sync skill directories with currently installed packages."""
    from apm_cli.integration.targets import KNOWN_TARGETS

    source = targets if targets is not None else list(KNOWN_TARGETS.values())
    stats = {"files_removed": 0, "errors": 0}
    skill_prefix_tuple = _build_skill_prefixes(source)
    if managed_files is not None:
        _sync_skills_managed_files(managed_files, project_root, skill_prefix_tuple, stats, source)
        return stats
    _sync_skills_legacy(
        apm_package, project_root, source, stats, link_rewriter._clean_orphaned_skills
    )
    return stats


def _clean_orphaned_skills(
    skills_dir: Path,
    installed_skill_names: set[str],
    *,
    project_root: Path | None = None,
    get_lockfile_owned_fn: Callable[[Path], set[str]] | None = None,
) -> dict[str, int]:
    """Clean orphaned skills from a skills directory."""
    files_removed = 0
    errors = 0
    is_agents_dir = skills_dir.parent.name == ".agents"
    lockfile_owned_skills: set[str] | None = None
    if is_agents_dir and project_root is not None:
        owner_fn = get_lockfile_owned_fn or _get_lockfile_owned_agent_skills
        lockfile_owned_skills = owner_fn(project_root)

    for skill_subdir in skills_dir.iterdir():
        if not skill_subdir.is_dir() or skill_subdir.name in installed_skill_names:
            continue
        if lockfile_owned_skills is not None and skill_subdir.name not in lockfile_owned_skills:
            continue
        try:
            shutil.rmtree(skill_subdir)
            files_removed += 1
        except Exception:
            errors += 1
    return {"files_removed": files_removed, "errors": errors}


def _get_lockfile_owned_agent_skills(project_root: Path) -> set[str]:
    """Return skill names under .agents/skills in the lockfile."""
    owned: set[str] = set()
    try:
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lockfile = LockFile.read(get_lockfile_path(project_root))
        if lockfile and lockfile.dependencies:
            for dep in lockfile.dependencies.values():
                for deployed_file in dep.deployed_files:
                    if deployed_file.startswith(".agents/skills/"):
                        parts = deployed_file[len(".agents/skills/") :].split("/")
                        if parts and parts[0]:
                            owned.add(parts[0])
    except (FileNotFoundError, OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
        _log.debug("Could not read lockfile for ownership check: %s", exc)
    return owned
