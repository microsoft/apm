"""Uninstall shrink of surviving shared selections (#3735, #4313).

The selection a survivor keeps is projected by ``engine.project_shrunk_selections``
through the canonical selection-merge owner; this module applies it and names
the deployed files the shrink retires, read from the deployment ledger, so the
evidence preflight can refuse before any change.
"""

from __future__ import annotations

from .engine import _is_private_local_identity, project_shrunk_selections


def _skill_root_sharers() -> dict[str, set[str]]:
    """Map each static skills root to every target that deploys skills there."""
    from ...integration.targets import KNOWN_TARGETS, scoped_profile_or_none

    sharers: dict[str, set[str]] = {}
    for profile in KNOWN_TARGETS.values():
        for scoped in (profile, scoped_profile_or_none(profile, user_scope=True)):
            if scoped is not None and scoped.supports("skills"):
                sharers.setdefault(scoped.skills_rel_root, set()).add(profile.name)
    return sharers


def _enclosing_skill_dir(value: str) -> str | None:
    from ...integration.cleanup import _is_skill_directory_entry

    path = value
    while path:
        if _is_skill_directory_entry(path):
            return path
        path = path.rpartition("/")[0]
    return None


def shrink_retired_paths(lockfile, shrunk: dict) -> set[str]:
    """Return the deployed paths the *shrunk* survivor selections stop deploying (#4313).

    Read from the deployment ledger: a skill directory (and its members)
    whose skill no surviving declaration selects, and every path none of
    whose targets the selection still covers. A skills root shared by several
    targets counts every target that deploys there.
    """
    from ...core.deployment_ledger import DeploymentLedgerCodec
    from ...models.dependency.subsets import skill_subset_filter_tokens

    if not lockfile or not shrunk:
        return set()
    records = DeploymentLedgerCodec.from_lockfile(lockfile).records.values()
    targets_at: dict[str, set[str]] = {}
    for record in records:
        targets_at.setdefault(record.locator.value.rstrip("/"), set()).add(record.locator.target)
    sharers = _skill_root_sharers()
    retired: set[str] = set()
    for key, node in shrunk.items():
        selection = node.lock_selection()
        skills = skill_subset_filter_tokens(selection.skill_subset)
        for value in {
            record.locator.value.rstrip("/") for record in records if key in record.owners
        }:
            skill_dir = _enclosing_skill_dir(value)
            targets = set(targets_at[value])
            if skill_dir is not None:
                targets |= sharers.get(skill_dir.rpartition("/")[0], set())
            if (
                selection.target_subset is not None
                and not any(selection.selects_target(target) for target in targets)
            ) or (
                skills is not None
                and skill_dir is not None
                and skill_dir.rpartition("/")[2] not in skills
            ):
                retired.add(value)
    return retired


def _shrink_surviving_selections(
    lockfile, packages_to_remove, apm_modules_dir, apm_yml_path, logger
) -> set[str]:
    """Narrow survivors' locked selections to what surviving edges still declare.

    Returns the lock keys whose selection changed; their deployed files must
    be removed and re-integrated with the new selection.
    """
    changed = set()
    shrunk = project_shrunk_selections(
        lockfile, packages_to_remove, apm_modules_dir, apm_yml_path, logger
    )
    for key, selection in shrunk.items():
        locked = lockfile.get_dependency(key)
        locked.skill_subset = list(selection.skill_subset or ())
        locked.target_subset = list(selection.target_subset or ())
        exact = selection.subset_pairings
        locked.subset_pairings = exact.to_lock_records() if exact is not None else None
        changed.add(key)
        label = "local dependency" if _is_private_local_identity(key) else key
        logger.verbose_detail(f"    Updated selection of {label} to its surviving declarations")
    return changed
