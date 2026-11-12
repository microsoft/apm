"""Supporting value objects and cleanup helpers for skill integration."""

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from apm_cli.core.deployment_state import MaterializationResult
from apm_cli.utils.path_security import has_symlink_component, safe_rmtree

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_link_like(path: str | Path) -> bool:
    """Treat symlinks and Windows reparse points (junctions) as links; fail closed."""
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True
    return os.path.islink(path) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def skill_dir_has_content(path: Path) -> bool:
    """Return whether a skill destination holds content; an empty directory tree does not.

    Fails closed: symlinks, junctions, any non-directory entry, and unreadable
    subtrees count as content so unmanaged material is never treated as absent.
    """
    if not os.path.lexists(path):
        return False
    if _is_link_like(path) or not path.is_dir():
        return True
    errors: list[OSError] = []
    for root, dirs, files in os.walk(path, onerror=errors.append):
        if files or errors or any(_is_link_like(os.path.join(root, d)) for d in dirs):
            return True
    return bool(errors)


def build_skill_ownership_maps(lockfile_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Read sub-skill names and native destinations into full-identity ownership maps."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    owned_by: dict[str, str] = {}
    native_owners: dict[str, str] = {}
    lockfile = LockFile.read(get_lockfile_path(lockfile_root))
    if not lockfile:
        return owned_by, native_owners
    from apm_cli.integration.targets import skills_root_prefixes

    prefixes = tuple(f"{root}/" for root in skills_root_prefixes())
    for dep in lockfile.get_package_dependencies():
        unique_key = dep.get_unique_key()
        for deployed_path in dep.deployed_files:
            normalized = deployed_path.rstrip("/").replace("\\", "/")
            skill_name = normalized.rsplit("/", 1)[-1]
            owned_by[skill_name] = unique_key
            # Another target's same-named skill cannot establish destination ownership.
            if "/skills/" in normalized or normalized.startswith(prefixes):
                native_owners[normalized] = unique_key
    return owned_by, native_owners


def build_copy_ignore(
    *,
    skip_bin: bool = False,
) -> Callable[[str, list[str]], list[str]]:
    """Build a ``shutil.copytree`` ignore function."""
    from apm_cli.security.gate import ignore_non_content

    if not skip_bin:
        return ignore_non_content
    bin_filter = shutil.ignore_patterns("bin")

    def combined(directory: str, contents: list[str]) -> list[str]:
        return list(
            set(ignore_non_content(directory, contents)) | set(bin_filter(directory, contents))
        )

    return combined


@dataclass
class SkillIntegrationResult:
    """Skill outputs and completion of the admitted skill-processing domain.

    ``complete`` is false unless all selected physical targets materialized every
    admitted source root/member, including final rewrites and hashes. It is not
    inferred from ``skill_skipped`` or the successful-file inventory, and does
    not certify other primitive domains or whole-package success.
    """

    skill_created: bool
    skill_updated: bool
    skill_skipped: bool
    skill_path: Path | None
    references_copied: int
    links_resolved: int = 0
    sub_skills_promoted: int = 0
    bin_deployed: int = 0
    bin_skipped_reason: str | None = None
    target_paths: list[Path] = None
    materializations: tuple[MaterializationResult, ...] = ()
    deployed_files: tuple[Path, ...] = ()
    complete: bool = False

    def __post_init__(self) -> None:
        if self.target_paths is None:
            self.target_paths = []


def clean_orphaned_skills(
    skills_dir: Path,
    installed_skill_names: set,
    *,
    project_root: Path | None,
    get_lockfile_owned_agent_skills: Callable[[Path], set[str]],
    removed_paths: list[Path] | None = None,
) -> dict[str, int]:
    """Remove legacy-orphan skill directories without touching foreign agents.

    Only regular directories directly inside an unlinked *skills_dir* are
    removed, and each removal stays contained in *project_root*: a linked
    skills root or skill entry is left untouched.
    """
    containment_root = project_root if project_root is not None else skills_dir
    if has_symlink_component(containment_root, skills_dir):
        return {"files_removed": 0, "errors": 0}
    protected_names = set(installed_skill_names)
    if project_root is not None:
        from apm_cli.integration.lsp_integrator import LSPIntegrator

        protected_names.update(LSPIntegrator.reserved_project_skill_names(skills_dir, project_root))
    files_removed = 0
    errors = 0
    lockfile_owned_skills: set[str] | None = None
    if skills_dir.parent.name == ".agents" and project_root is not None:
        lockfile_owned_skills = get_lockfile_owned_agent_skills(project_root)

    for skill_subdir in skills_dir.iterdir():
        if skill_subdir.is_symlink() or not skill_subdir.is_dir():
            continue
        if skill_subdir.name in protected_names:
            continue
        if lockfile_owned_skills is not None and skill_subdir.name not in lockfile_owned_skills:
            continue
        try:
            safe_rmtree(skill_subdir, containment_root)
            files_removed += 1
            if removed_paths is not None:
                removed_paths.append(skill_subdir)
        except Exception:
            errors += 1

    return {"files_removed": files_removed, "errors": errors}


def get_lockfile_owned_agent_skills(project_root: Path) -> set[str]:
    """Return APM-owned ``.agents/skills`` names from the lockfile."""
    owned: set[str] = set()
    try:
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lockfile = LockFile.read(get_lockfile_path(project_root))
        if lockfile and lockfile.dependencies:
            for dep in lockfile.dependencies.values():
                for deployed_file in dep.deployed_files:
                    if deployed_file.startswith(".agents/skills/"):
                        name = deployed_file[len(".agents/skills/") :].split("/", 1)[0]
                        if name:
                            owned.add(name)
    except (FileNotFoundError, OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
        import logging

        logging.getLogger(__name__).debug("Could not read lockfile for ownership check: %s", exc)
    return owned
