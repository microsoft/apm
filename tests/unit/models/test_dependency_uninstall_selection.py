"""Canonical dependency selection tests for uninstall identifiers."""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.selection import (
    DependencySelectionStatus,
    select_manifest_dependency,
)


def _local_lock(*paths: str, alias: str) -> LockFile:
    dependencies = {}
    for path in paths:
        dependency = LockedDependency(
            repo_url=alias,
            source="local",
            local_path=path,
        )
        dependencies[dependency.get_unique_key()] = dependency
    return LockFile(dependencies=dependencies)


@pytest.mark.parametrize(
    "declared_path",
    (
        "../Packages/Mixed Case Package",
        "/opt/apm packages/Mixed Case Package",
    ),
)
def test_portable_local_alias_selects_manifest_entry_from_lock_metadata(
    declared_path: str,
) -> None:
    """The deps-list alias must select relative and absolute declarations."""
    alias = "_local/Mixed Case Package"
    selection = select_manifest_dependency(
        alias,
        [declared_path],
        _local_lock(declared_path, alias=alias),
    )

    assert selection.status is DependencySelectionStatus.MATCHED
    assert selection.manifest_entry == declared_path
    assert selection.match_count == 1


def test_portable_alias_without_matching_lock_metadata_does_not_guess() -> None:
    """A remote-looking token alone must not be reinterpreted as a local path."""
    selection = select_manifest_dependency(
        "_local/shared",
        ["../one/shared"],
        lockfile=None,
    )

    assert selection.status is DependencySelectionStatus.NOT_FOUND
    assert selection.manifest_entry is None
    assert selection.match_count == 0


def test_duplicate_portable_alias_is_ambiguous_but_exact_paths_still_select() -> None:
    """Two declared paths with one basename must never be guessed or co-removed."""
    first = "../one/Shared Package"
    second = "../two/Shared Package"
    alias = "_local/Shared Package"
    lockfile = _local_lock(first, second, alias=alias)

    ambiguous = select_manifest_dependency(alias, [first, second], lockfile)
    exact = select_manifest_dependency(second, [first, second], lockfile)

    assert ambiguous.status is DependencySelectionStatus.AMBIGUOUS
    assert ambiguous.manifest_entry is None
    assert ambiguous.match_count == 2
    assert exact.status is DependencySelectionStatus.MATCHED
    assert exact.manifest_entry == second
    assert exact.match_count == 1


def test_remote_dependency_named_local_owner_keeps_exact_identity_semantics() -> None:
    """A real remote _local/repo declaration remains directly uninstallable."""
    selection = select_manifest_dependency(
        "_local/remote-package",
        ["_local/remote-package"],
        lockfile=None,
    )

    assert selection.status is DependencySelectionStatus.MATCHED
    assert selection.manifest_entry == "_local/remote-package"


@pytest.mark.windows_compat
def test_windows_declared_path_accepts_portable_lock_alias() -> None:
    """Windows local paths match by persisted identity on every host OS."""
    declared_path = r"C:\APM Packages\Mixed Case Package"
    alias = "_local/Mixed Case Package"
    assert DependencyReference.parse(declared_path).repo_url == alias

    selection = select_manifest_dependency(
        alias,
        [declared_path],
        _local_lock(declared_path, alias=alias),
    )

    assert selection.status is DependencySelectionStatus.MATCHED
    assert selection.manifest_entry == declared_path
