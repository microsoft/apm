"""Supporting value objects and cleanup helpers for skill integration."""

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core.deployment_state import MaterializationResult

if TYPE_CHECKING:
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan

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

    _combined.rebased = lambda plan: _build_deployable_copy_ignore(  # type: ignore[attr-defined]
        skip_bin=skip_bin,
        source_plan=plan,
        exclude_apm=exclude_apm,
        exclude_apm_yml=exclude_apm_yml,
    )
    return _combined


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
    # Skills whose deployed bytes changed this run; None when not measured.
    skills_changed: int | None = None
    # Of the promoted skills, those whose identical pre-existing copy was adopted.
    sub_skills_adopted: int = 0
    bin_deployed: int = 0
    bin_skipped_reason: str | None = None
    target_paths: list[Path] = None
    materializations: tuple[MaterializationResult, ...] = ()
    deployed_files: tuple[Path, ...] = ()
    complete: bool = False

    def __post_init__(self) -> None:
        if self.target_paths is None:
            self.target_paths = []


def skills_integrated(result: SkillIntegrationResult) -> int:
    """Skills one package integrated: changed skills plus adopted ones.

    ``SkillIntegrator`` measures ``skills_changed`` -- skills whose deployed
    bytes changed (new or rewritten) -- so a converged skill directory counts
    zero (#4184); an identical unowned copy it adopts counts once, reported as
    adopted. Results that do not measure it count a new root skill plus
    promoted skills; a ``skills/`` bundle has no root skill (#4089).
    """
    if isinstance(result.skills_changed, int):
        raw_adopted = getattr(result, "sub_skills_adopted", 0)
        return result.skills_changed + (raw_adopted if isinstance(raw_adopted, int) else 0)
    root_skill = 1 if result.skill_created and result.skill_path is not None else 0
    return root_skill + result.sub_skills_promoted


def tree_file_hashes(root: Path, key: Callable[[Path], str]) -> dict[str, str]:
    """Hash the regular files already under *root*, keyed like deployment receipts.

    Taken before a skill copy so the integrator can tell a rewrite that changed
    bytes from one that left the destination identical. Links are never read.
    """
    from apm_cli.utils.content_hash import compute_file_hash

    hashes: dict[str, str] = {}
    if not root.is_dir() or _is_link_like(root):
        return hashes
    for directory, _dirs, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            if not _is_link_like(path) and path.is_file():
                hashes[key(path)] = compute_file_hash(path)
    return hashes


def skill_line_label(result: SkillIntegrationResult, skill_count: int) -> str:
    """Label one package's skill line, reporting identical pre-existing copies as adopted."""
    promoted = result.sub_skills_promoted
    raw_adopted = getattr(result, "sub_skills_adopted", 0)
    adopted = min(raw_adopted if isinstance(raw_adopted, int) else 0, skill_count)
    if adopted == skill_count:
        return f"{adopted} skill(s) adopted"
    integrated = skill_count - adopted
    if integrated == 1 and not promoted:
        return "Skill integrated"
    phrase = f"{integrated} skill(s) integrated"
    return f"{phrase} ({adopted} adopted)" if adopted else phrase
