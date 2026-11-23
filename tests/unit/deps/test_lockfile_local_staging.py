"""Local bundle staging is lockfile-proven compile input, not a deployment claim (#3705)."""

from __future__ import annotations

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockFile

pytestmark = pytest.mark.unit

_SKILL = ".agents/skills/helper/SKILL.md"
_STAGED = "apm_modules/bundler/.apm/instructions/glob.instructions.md"
_HASH = "sha256:" + "a" * 64


def test_pre_3705_staging_claims_converge_on_reinstall() -> None:
    lock = LockFile()
    DeploymentLedgerCodec.record_local_bundle_files(
        lock, [_SKILL, _STAGED], {_SKILL: _HASH, _STAGED: _HASH}, scope=InstallScope.PROJECT
    )
    assert _STAGED in DeploymentLedgerCodec.local_bundle_paths(lock)
    assert lock.local_staging_slugs() == ["bundler"]

    DeploymentLedgerCodec.record_local_bundle_files(
        lock, [_SKILL], {_SKILL: _HASH}, staged=[_STAGED], scope=InstallScope.PROJECT
    )

    assert lock.local_deployed_files == [_SKILL]
    assert _STAGED not in lock.local_deployed_file_hashes
    assert DeploymentLedgerCodec.local_bundle_paths(lock) == {_SKILL}
    assert lock.local_staged_files == [_STAGED]


def test_staging_is_retired_with_its_pruned_root() -> None:
    other = "apm_modules/other/.apm/instructions/x.md"
    lock = LockFile(
        local_staged_files=[_STAGED, other, "", "stray.md"],
        local_staged_file_hashes={_STAGED: _HASH, other: _HASH},
    )
    lock.retire_local_staging(["bundler"])
    assert lock.local_staged_files == [other, "", "stray.md"]
    assert lock.local_staged_file_hashes == {other: _HASH}
    assert lock.local_staging_slugs() == ["other"]


def test_staged_hashes_carry_with_their_records_and_bundle_fallback_never_wins() -> None:
    other = "apm_modules/bundler/.apm/instructions/other.md"
    prior = LockFile(
        local_staged_files=[_STAGED], local_staged_file_hashes={_STAGED: _HASH, other: _HASH}
    )
    carried = LockFile()
    carried.carry_local_staging_from(prior)
    assert carried.local_staged_files == [_STAGED]
    assert carried.local_staged_file_hashes == {_STAGED: _HASH}

    bundle = "sha256:" + "b" * 64
    DeploymentLedgerCodec.record_local_bundle_files(
        carried,
        [],
        {},
        staged_fallback_hashes={_STAGED: bundle, other: bundle},
        scope=InstallScope.PROJECT,
    )
    assert carried.local_staged_file_hashes == {_STAGED: _HASH}
    unhashed = LockFile(local_staged_files=[_STAGED])
    DeploymentLedgerCodec.record_local_bundle_files(
        unhashed, [], {}, staged_fallback_hashes={_STAGED: bundle}, scope=InstallScope.PROJECT
    )
    assert unhashed.local_staged_file_hashes == {_STAGED: bundle}


def test_staging_slugs_reject_unsafe_roots() -> None:
    unsafe = [
        "apm_modules/../.apm/instructions/x.md",
        "apm_modules/./.apm/instructions/x.md",
        "apm_modules//.apm/instructions/x.md",
        "/abs/.apm/instructions/x.md",
        "apm_modules/C:/.apm/instructions/x.md",
        "apm_modules/a\\b/.apm/instructions/x.md",
        "apm_modules/C:\\evil/.apm/instructions/x.md",
    ]
    lock = LockFile(local_staged_files=[*unsafe, _STAGED], local_deployed_files=unsafe)
    assert lock.local_staging_slugs() == ["bundler"]


def test_pre_3705_staging_claims_migrate_even_when_not_restaged() -> None:
    """A newer bundle that omits or skips an instruction still retires its old claim."""
    lock = LockFile()
    DeploymentLedgerCodec.record_local_bundle_files(
        lock, [_SKILL, _STAGED], {_SKILL: _HASH, _STAGED: _HASH}, scope=InstallScope.PROJECT
    )

    DeploymentLedgerCodec.record_local_bundle_files(
        lock, [_SKILL], {_SKILL: _HASH}, scope=InstallScope.PROJECT
    )

    assert lock.local_deployed_files == [_SKILL]
    assert DeploymentLedgerCodec.local_bundle_paths(lock) == {_SKILL}
    assert lock.local_staged_files == [_STAGED]
    # The claim's install-time hash moves with it, so a skipped edit stays visible.
    assert lock.local_staged_file_hashes == {_STAGED: _HASH}
