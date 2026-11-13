"""Local bundle staging is lockfile-proven compile input, not a deployment claim (#3705)."""

from __future__ import annotations

from pathlib import Path

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile

_SKILL = ".agents/skills/helper/SKILL.md"
_STAGED = "apm_modules/bundler/.apm/instructions/glob.instructions.md"
_HASH = "sha256:" + "a" * 64


def test_staged_files_are_recorded_apart_from_claims_and_round_trip(tmp_path: Path) -> None:
    lock = LockFile()
    DeploymentLedgerCodec.record_local_bundle_files(
        lock, [_SKILL], {_SKILL: _HASH}, staged=[_STAGED]
    )

    assert lock.local_deployed_files == [_SKILL]
    assert lock.local_staged_files == [_STAGED]
    assert DeploymentLedgerCodec.local_bundle_paths(lock) == {_SKILL}
    assert lock.local_staging_slugs() == ["bundler"]

    path = tmp_path / "apm.lock.yaml"
    lock.write(path)
    restored = LockFile.read(path)
    assert restored is not None
    assert restored.local_staged_files == [_STAGED]
    assert restored.is_semantically_equivalent(lock)


def test_pre_3705_staging_claims_converge_on_reinstall() -> None:
    lock = LockFile()
    DeploymentLedgerCodec.record_local_bundle_files(
        lock, [_SKILL, _STAGED], {_SKILL: _HASH, _STAGED: _HASH}
    )
    assert _STAGED in DeploymentLedgerCodec.local_bundle_paths(lock)
    assert lock.local_staging_slugs() == ["bundler"]

    DeploymentLedgerCodec.record_local_bundle_files(
        lock, [_SKILL], {_SKILL: _HASH}, staged=[_STAGED]
    )

    assert lock.local_deployed_files == [_SKILL]
    assert _STAGED not in lock.local_deployed_file_hashes
    assert DeploymentLedgerCodec.local_bundle_paths(lock) == {_SKILL}
    assert lock.local_staged_files == [_STAGED]


def test_staging_is_retired_with_its_pruned_root() -> None:
    other = "apm_modules/other/.apm/instructions/x.md"
    lock = LockFile(local_staged_files=[_STAGED, other, "", "stray.md"])
    lock.retire_local_staging(["bundler"])
    assert lock.local_staged_files == [other, "", "stray.md"]
    assert lock.local_staging_slugs() == ["other"]
