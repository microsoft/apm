"""Uninstall shrink of surviving shared selections (#3735, #4313).

The selection a survivor keeps is projected by ``engine.project_shrunk_selections``
through the canonical selection-merge owner; this module applies it and names
the deployed files the shrink retires, read from the deployment ledger, so the
evidence preflight can refuse before any change.
"""

from __future__ import annotations

from .engine import _is_private_local_identity, project_shrunk_selections


def _skill_root_sharers() -> dict[str, set[str]]:
    """Map each static skills root to every target that deploys skills there.

    Dynamic-root targets are skipped, as in ``skills_root_prefixes``: their
    URI locators already carry the recorded target, and resolving their user
    root here could fail on an unrelated machine configuration.
    """
    from ...integration.targets import KNOWN_TARGETS, scoped_profile_or_none

    sharers: dict[str, set[str]] = {}
    for profile in KNOWN_TARGETS.values():
        if profile.user_root_resolver is not None:
            continue
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


def _still_selected(selection, targets: set[str], skill: str | None) -> bool:
    """Return whether *selection* still deploys a path of *skill* for one of *targets*.

    Checked per target through the exact pairings, so a skill selected for
    one target never keeps another target's copy of it alive.
    """
    from ...models.dependency.subsets import skill_subset_filter_tokens

    for target in targets:
        if not selection.selects_target(target):
            continue
        if skill is None:
            return True
        chosen = skill_subset_filter_tokens(selection.skills_for_target(target))
        if chosen is None or skill in chosen:
            return True
    return False


def shrink_path_split(lockfile, shrunk: dict) -> tuple[set[str], set[str]]:
    """Return ``(retired, retained)`` deployed paths of the *shrunk* survivors (#4313).

    Read from the deployment ledger: a path is retired when no target whose
    root holds it still selects it (and, inside a skill directory, its skill
    for that target). A skills root shared by several targets counts every
    target that deploys there. A retired skill directory also retires the
    hash-tracked members older lockfiles record only in
    ``deployed_file_hashes``. Every other owned path stays deployed.
    """
    from ...core.deployment_ledger import DeploymentLedgerCodec

    if not lockfile or not shrunk:
        return set(), set()
    records = DeploymentLedgerCodec.from_lockfile(lockfile).records.values()
    targets_at: dict[str, set[str]] = {}
    for record in records:
        targets_at.setdefault(record.locator.value.rstrip("/"), set()).add(record.locator.target)
    sharers = _skill_root_sharers()
    retired: set[str] = set()
    retained: set[str] = set()
    for key, node in shrunk.items():
        selection = node.lock_selection()
        locked = lockfile.get_dependency(key)
        retired_dirs: set[str] = set()
        for value in {
            record.locator.value.rstrip("/") for record in records if key in record.owners
        }:
            skill_dir = _enclosing_skill_dir(value)
            targets = set(targets_at[value])
            if skill_dir is not None:
                targets |= sharers.get(skill_dir.rpartition("/")[0], set())
            skill = skill_dir.rpartition("/")[2] if skill_dir is not None else None
            if _still_selected(selection, targets, skill):
                retained.add(value)
                continue
            retired.add(value)
            if skill_dir == value:
                retired_dirs.add(value)
        hashed = getattr(locked, "deployed_file_hashes", None) or {}
        retired.update(
            path for path in hashed if any(path.startswith(d + "/") for d in retired_dirs)
        )
    return retired, retained


def shrink_retired_paths(lockfile, shrunk: dict) -> set[str]:
    """Return the deployed paths the *shrunk* survivor selections stop deploying (#4313)."""
    return shrink_path_split(lockfile, shrunk)[0]


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
