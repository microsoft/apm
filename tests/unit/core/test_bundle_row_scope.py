"""User-scope local-bundle rows keep ``scope: user`` across ledger rebuilds (#3995)."""

from __future__ import annotations

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = pytest.mark.unit

_BUNDLED = ".claude/skills/bundled/SKILL.md"
_HASH = "sha256:" + "b" * 64


def _bundle_rows(lockfile: LockFile) -> list[dict]:
    return [
        row
        for row in DeploymentLedgerCodec.rows(lockfile.deployment_ledger)
        if row["value"] == _BUNDLED
    ]


def _user_bundle_lock() -> LockFile:
    lockfile = LockFile()
    DeploymentLedgerCodec.record_local_bundle_files(
        lockfile, [_BUNDLED], {_BUNDLED: _HASH}, scope=InstallScope.USER
    )
    return lockfile


def test_bundle_writer_requires_an_explicit_scope() -> None:
    with pytest.raises(TypeError):
        DeploymentLedgerCodec.record_local_bundle_files(  # type: ignore[call-arg]
            LockFile(), [_BUNDLED], {_BUNDLED: _HASH}
        )


@pytest.mark.parametrize("scope", [InstallScope.USER, InstallScope.PROJECT])
def test_bundle_rows_record_the_install_scope(scope: InstallScope) -> None:
    lockfile = LockFile()
    DeploymentLedgerCodec.record_local_bundle_files(
        lockfile, [_BUNDLED], {_BUNDLED: _HASH}, scope=scope
    )
    (row,) = _bundle_rows(lockfile)
    assert row["scope"] == scope.value
    assert row["active_owner"] == "local-bundle"
    assert row["content_hash"] == _HASH


def test_unrelated_rebuild_keeps_user_scope_owners_and_hash() -> None:
    """Regression trap for #3995: the rebuild used to re-derive ``project``."""
    lockfile = _user_bundle_lock()
    before = _bundle_rows(lockfile)
    lockfile.dependencies["owner/other"] = LockedDependency(repo_url="owner/other")
    DeploymentLedgerCodec.replace_legacy_owner(
        lockfile, "owner/other", [".claude/skills/other/SKILL.md"], {}
    )
    DeploymentLedgerCodec.replace_legacy_owners(
        lockfile, {"owner/other": ([".claude/skills/other/SKILL.md"], {})}
    )
    assert _bundle_rows(lockfile) == before
    assert before[0]["scope"] == "user"


def test_supplied_provenance_carries_the_bundle_scope() -> None:
    """A freshly built lockfile takes the bundle claim's scope from provenance."""
    provenance = _user_bundle_lock().deployment_ledger
    fresh = LockFile()
    DeploymentLedgerCodec.replace_legacy_owner(
        fresh, ".", [_BUNDLED], {_BUNDLED: _HASH}, provenance=provenance
    )
    (row,) = _bundle_rows(fresh)
    assert (row["scope"], row["active_owner"]) == ("user", "local-bundle")


def test_user_rows_stay_out_of_project_lookups() -> None:
    (record,) = (
        record
        for record in _user_bundle_lock().deployment_ledger.records.values()
        if record.locator.value == _BUNDLED
    )
    assert DeploymentLedgerCodec.locator_in_scope(record.locator, "user")
    assert not DeploymentLedgerCodec.locator_in_scope(record.locator, "project")


def _with_second_scope(ledger, scope: str):
    """Add the bundle claim again under *scope*, as an ambiguous source would hold it."""
    from dataclasses import replace

    from apm_cli.core.deployment_state import DeploymentLedger

    records = dict(ledger.records)
    (record,) = (r for r in records.values() if r.locator.value == _BUNDLED)
    twin = replace(record, locator=replace(record.locator, scope=scope))
    records[twin.locator.key] = twin
    return DeploymentLedger(records=records)


def test_ambiguous_claim_in_one_source_keeps_the_path_inferred_scope() -> None:
    """A path claimed with two scopes is never defaulted to user (#3995)."""
    lockfile = _user_bundle_lock()
    lockfile.deployment_ledger = _with_second_scope(lockfile.deployment_ledger, "project")
    assert DeploymentLedgerCodec._bundle_scopes((lockfile.deployment_ledger,)) == {}
    lockfile.dependencies["owner/other"] = LockedDependency(repo_url="owner/other")
    DeploymentLedgerCodec.replace_legacy_owner(
        lockfile, "owner/other", [".claude/skills/other/SKILL.md"], {}
    )
    (row,) = _bundle_rows(lockfile)
    assert row["scope"] == DeploymentLedgerCodec.legacy_scope(_BUNDLED) == "project"
    assert (row["active_owner"], row["content_hash"]) == ("local-bundle", _HASH)


def test_prior_ledger_scope_wins_over_disagreeing_provenance() -> None:
    lockfile = _user_bundle_lock()
    project = LockFile()
    DeploymentLedgerCodec.record_local_bundle_files(
        project, [_BUNDLED], {_BUNDLED: _HASH}, scope=InstallScope.PROJECT
    )
    DeploymentLedgerCodec.replace_legacy_owner(
        lockfile, ".", [_BUNDLED], {_BUNDLED: _HASH}, provenance=project.deployment_ledger
    )
    (row,) = _bundle_rows(lockfile)
    assert row["scope"] == "user"
