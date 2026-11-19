"""Canonical dependency selection tests for uninstall identifiers."""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.selection import (
    DependencySelectionStatus,
    manifest_declarations_of,
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


def test_one_package_declared_twice_is_one_match() -> None:
    """#3693: duplicate declarations of one identity never make uninstall ambiguous."""
    declarations = ["acme/c", "https://github.com/acme/c.git", {"git": "acme/c"}, "acme/d"]

    selection = select_manifest_dependency("acme/c", declarations, lockfile=None)

    assert selection.status is DependencySelectionStatus.MATCHED
    assert selection.manifest_entry == "acme/c"
    assert selection.match_count == 3
    assert manifest_declarations_of(declarations, ["git@github.com:acme/c.git"]) == declarations[:3]


def test_declarations_of_distinct_local_packages_stay_separate() -> None:
    assert manifest_declarations_of(["../one/pkg", "../two/pkg"], ["../two/pkg"]) == ["../two/pkg"]


def test_marketplace_ref_selects_object_form_declaration_case_insensitively() -> None:
    """``plugin@alias`` names an object-form entry without lock or registry (#3928)."""
    from apm_cli.models.dependency.selection import select_marketplace_declaration

    declared = {"name": "example-plugin", "marketplace": "company"}
    manifest = ["acme/other", declared]

    selection = select_marketplace_declaration("Example-Plugin", "COMPANY", manifest)
    assert selection.status is DependencySelectionStatus.MATCHED
    assert selection.manifest_entry is declared

    other_alias = select_marketplace_declaration("example-plugin", "elsewhere", manifest)
    assert other_alias.status is DependencySelectionStatus.NOT_FOUND
    string_form = select_marketplace_declaration("other", "acme", manifest)
    assert string_form.status is DependencySelectionStatus.NOT_FOUND


def test_object_form_removal_targets_the_locked_coordinate() -> None:
    """Lock row, checkout and edges of an object-form entry use its locked coordinate."""
    from apm_cli.commands.uninstall.engine import _removal_target, _removed_lock_key

    locked = LockedDependency(
        repo_url="team/catalog",
        host="git.example.com",
        virtual_path="plugins/example-plugin",
        is_virtual=True,
        resolved_commit="a" * 40,
        discovered_via="company",
        marketplace_plugin_name="example-plugin",
    )
    lockfile = LockFile(dependencies={locked.get_unique_key(): locked})
    declared = {"name": "example-plugin", "marketplace": "company"}

    assert _removed_lock_key(declared, lockfile) == locked.get_unique_key()
    target = _removal_target(declared, lockfile)
    assert isinstance(target, DependencyReference)
    assert target.get_unique_key() == locked.get_unique_key()
    assert _removal_target(declared, LockFile()) is declared
    assert _removal_target("acme/pkg", lockfile) == "acme/pkg"


def _object_form_lock() -> tuple[LockFile, LockedDependency]:
    locked = LockedDependency(
        repo_url="team/catalog",
        host="git.example.com",
        virtual_path="plugins/example-plugin",
        is_virtual=True,
        resolved_commit="a" * 40,
        discovered_via="company",
        marketplace_plugin_name="example-plugin",
    )
    return LockFile(dependencies={locked.get_unique_key(): locked}), locked


def test_removing_one_of_two_declarations_of_one_package_fails_closed() -> None:
    """An object-form entry and a Git entry for the same package go together or not at all."""
    from unittest.mock import MagicMock

    from apm_cli.commands.uninstall.engine import _refuse_unsafe_marketplace_removals

    lockfile, locked = _object_form_lock()
    declared = {"name": "example-plugin", "marketplace": "company"}
    git_form = locked.get_unique_key()
    manifest = [declared, git_form]
    logger = MagicMock()

    refused = _refuse_unsafe_marketplace_removals([declared], manifest, lockfile, logger)
    assert refused == ["example-plugin@company"]
    message = logger.error.call_args.args[0]
    assert git_form in message and "apm uninstall example-plugin@company" in message

    logger.reset_mock()
    both = _refuse_unsafe_marketplace_removals([declared, git_form], manifest, lockfile, logger)
    assert both == []
    logger.error.assert_not_called()


def test_dry_run_previews_object_form_checkout_from_the_scoped_lockfile(
    tmp_path, monkeypatch
) -> None:
    """The preview uses the caller's lockfile, not ./apm.lock.yaml (global scope)."""
    from unittest.mock import MagicMock

    from apm_cli.commands.uninstall.engine import _dry_run_uninstall

    lockfile, locked = _object_form_lock()
    modules = tmp_path / "home" / "apm_modules"
    checkout = locked.to_dependency_ref().get_install_path(modules)
    checkout.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)  # no apm.lock.yaml here
    logger = MagicMock()

    _dry_run_uninstall(
        [{"name": "example-plugin", "marketplace": "company"}],
        modules,
        logger,
        lockfile=lockfile,
    )
    lines = [call.args[0] for call in logger.progress.call_args_list]
    assert any("from apm_modules/" in line for line in lines)


def test_recased_alias_still_targets_the_locked_row() -> None:
    """A lock row written as ``Company`` still owns the ``company`` declaration."""
    from apm_cli.commands.uninstall.engine import _removed_lock_key

    lockfile, locked = _object_form_lock()
    locked.discovered_via = "Company"
    declared = {"name": "example-plugin", "marketplace": "company"}

    assert _removed_lock_key(declared, lockfile) == locked.get_unique_key()


def test_ambiguous_lock_rows_for_one_declaration_fail_closed() -> None:
    """Two lock rows for one alias/plugin pair cannot be mapped, so nothing is removed."""
    from unittest.mock import MagicMock

    from apm_cli.commands.uninstall.engine import _refuse_unsafe_marketplace_removals

    lockfile, _locked = _object_form_lock()
    twin = LockedDependency(
        repo_url="team/other-catalog",
        host="git.example.com",
        resolved_commit="b" * 40,
        discovered_via="company",
        marketplace_plugin_name="example-plugin",
    )
    lockfile.dependencies[twin.get_unique_key()] = twin
    declared = {"name": "example-plugin", "marketplace": "company"}
    logger = MagicMock()

    refused = _refuse_unsafe_marketplace_removals([declared], [declared], lockfile, logger)
    assert refused == ["example-plugin@company"]
    assert "records 2 packages" in logger.error.call_args.args[0]


def test_object_form_removal_seeds_the_orphan_scan_from_the_locked_plugin() -> None:
    """The plugin's own dependencies become orphan candidates when it is removed."""
    from apm_cli.commands.uninstall.engine import _removal_targets
    from apm_cli.deps.lockfile import lockfile_parent_identity
    from apm_cli.install.orphans import collect_orphan_candidates

    lockfile, locked = _object_form_lock()
    child = LockedDependency(
        repo_url="acme/child",
        resolved_commit="c" * 40,
        depth=2,
        resolved_by=lockfile_parent_identity(locked, lockfile.replacements),
    )
    lockfile.dependencies[child.get_unique_key()] = child
    declared = {"name": "example-plugin", "marketplace": "company"}

    candidates, _parents = collect_orphan_candidates(
        lockfile, _removal_targets([declared], lockfile)
    )
    assert child.get_unique_key() in candidates
    unmapped, _ = collect_orphan_candidates(lockfile, [declared])
    assert child.get_unique_key() not in unmapped
