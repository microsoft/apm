"""Selected-profile uninstall for targets whose user root a variable selects.

Claude (``CLAUDE_CONFIG_DIR``) and Hermes (``HERMES_HOME``) profiles share
one user manifest and lockfile. A global uninstall removes a package only
from the selected profile: when lockfile rows of the package remain under
another profile root, the manifest entry, ``apm_modules/`` copy, and those
rows stay so the other profile keeps working (#3425).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable


def other_profile_rows(lockfile, dependency_keys: Collection[str]) -> dict[str, frozenset[str]]:
    """Return each key's lockfile rows recorded under another profile root.

    A row belongs to another profile when its ledger record names a target
    of :data:`USER_ROOT_ENV_VARS` and the value lies outside that target's
    selected root (a lexical check through ``is_other_profile_path``).
    """
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.integration.targets import is_other_profile_path, selected_user_profiles

    profiles = selected_user_profiles()
    keys = {key for key in dependency_keys if key in lockfile.dependencies}
    rows: dict[str, set[str]] = {}
    for record in DeploymentLedgerCodec.from_lockfile(lockfile).records.values():
        profile = profiles.get(record.locator.target)
        value = record.locator.value
        if profile is None or not is_other_profile_path(profile, value):
            continue
        for owner in keys.intersection(record.owners):
            if value in lockfile.dependencies[owner].deployed_files:
                rows.setdefault(owner, set()).add(value)
    return {key: frozenset(values) for key, values in rows.items()}


def split_profile_retained(packages: Iterable, lockfile, key_of) -> tuple[list, list]:
    """Split manifest entries into full removals and selected-profile removals.

    *key_of* maps a manifest entry to its lockfile key. An entry whose
    package still has rows under another profile root is retained.
    """
    entries = list(packages)
    keys = {key_of(entry) for entry in entries}
    deployed_elsewhere = other_profile_rows(lockfile, keys)
    removed = [entry for entry in entries if key_of(entry) not in deployed_elsewhere]
    retained = [entry for entry in entries if key_of(entry) in deployed_elsewhere]
    return removed, retained


def selected_profile_survivors(lockfile, dependency_keys: Collection[str]) -> set[str]:
    """Return rows of *dependency_keys* that a selected-profile uninstall keeps.

    Only rows under a selected profile root are removed; every other row
    (another profile, another target) survives.
    """
    from apm_cli.integration.targets import get_integration_prefixes, selected_user_profiles

    prefixes = tuple(
        prefix.replace("\\", "/")
        for prefix in get_integration_prefixes(targets=list(selected_user_profiles().values()))
    )
    return {
        row
        for key in dependency_keys
        if key in lockfile.dependencies
        for row in lockfile.dependencies[key].deployed_files
        if "://" in row or not row.replace("\\", "/").startswith(prefixes)
    }


def retire_selected_profile_rows(
    lockfile, dependency_keys: Collection[str], survivors: Collection[str]
) -> bool:
    """Drop the selected profile's rows of *dependency_keys* from *lockfile*.

    Call only after cleanup removed those files. Returns whether rows changed.
    """
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    kept = set(survivors)
    updates: dict[str, tuple[list[str], dict[str, str]]] = {}
    for key in sorted(dependency_keys):
        dependency = lockfile.dependencies.get(key)
        if dependency is None:
            continue
        files = [row for row in dependency.deployed_files if row in kept]
        if files == list(dependency.deployed_files):
            continue
        hashes = {
            row: value for row, value in dependency.deployed_file_hashes.items() if row in kept
        }
        updates[key] = (files, hashes)
    DeploymentLedgerCodec.replace_legacy_owners(lockfile, updates)
    return bool(updates)
