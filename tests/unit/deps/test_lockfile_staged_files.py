"""Staged local files recorded in the lockfile and their hash drift."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.content_hash import compute_file_hash

pytestmark = pytest.mark.component

_SKILL = ".agents/skills/helper/SKILL.md"
_STAGED = "apm_modules/bundler/.apm/instructions/glob.instructions.md"
_HASH = "sha256:" + "a" * 64


def test_staged_files_are_recorded_apart_from_claims_and_round_trip(tmp_path: Path) -> None:
    lock = LockFile()
    DeploymentLedgerCodec.record_local_bundle_files(
        lock,
        [_SKILL],
        {_SKILL: _HASH},
        staged=[_STAGED],
        staged_hashes={_STAGED: _HASH},
        scope=InstallScope.PROJECT,
    )

    assert lock.local_deployed_files == [_SKILL]
    assert lock.local_staged_files == [_STAGED]
    assert lock.local_staged_file_hashes == {_STAGED: _HASH}
    assert DeploymentLedgerCodec.local_bundle_paths(lock) == {_SKILL}
    assert lock.local_staging_slugs() == ["bundler"]

    path = tmp_path / "apm.lock.yaml"
    lock.write(path)
    restored = LockFile.read(path)
    assert restored is not None
    assert restored.local_staged_files == [_STAGED]
    assert restored.local_staged_file_hashes == {_STAGED: _HASH}
    assert restored.is_semantically_equivalent(lock)
    assert not restored.is_semantically_equivalent(
        LockFile.from_yaml(lock.to_yaml().replace(_HASH, "sha256:" + "b" * 64))
    )


def test_absent_staging_slugs_only_reports_recorded_missing_roots(tmp_path: Path) -> None:
    modules = tmp_path / "apm_modules"
    (modules / "present").mkdir(parents=True)
    lock = LockFile(
        local_staged_files=[_STAGED, "apm_modules/present/.apm/instructions/x.md"],
        local_deployed_files=["apm_modules/legacy/.apm/instructions/y.md"],
    )
    assert lock.absent_staging_slugs(modules) == ["bundler"]


def test_staged_hash_drift_reports_only_hash_bound_staging(tmp_path: Path) -> None:
    """Only recorded staging with a hash makes a drift claim (#3718)."""
    unhashed = "apm_modules/bundler/.apm/instructions/old.md"
    escaped = "apm_modules/bundler/.apm/../../escape.md"
    for rel in (_STAGED, unhashed, "apm_modules/escape.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("installed\n", encoding="ascii")
    digest = compute_file_hash(tmp_path / _STAGED)
    lock = LockFile(
        local_staged_files=[_STAGED, unhashed, escaped, "apm_modules/bundler/.apm/gone.md"],
        local_staged_file_hashes={
            _STAGED: digest,
            escaped: _HASH,
            "apm_modules/bundler/.apm/gone.md": _HASH,
        },
    )
    assert lock.staged_hash_drift(tmp_path) == []

    (tmp_path / _STAGED).write_text("edited\n", encoding="ascii")
    (tmp_path / unhashed).write_text("edited\n", encoding="ascii")
    assert lock.staged_hash_drift(tmp_path) == [
        (_STAGED, digest, compute_file_hash(tmp_path / _STAGED))
    ]


def test_staged_hash_drift_skips_staging_resolving_outside_modules(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / ".apm" / "instructions"
    outside.mkdir(parents=True)
    (outside / "glob.instructions.md").write_text("edited\n", encoding="ascii")
    (tmp_path / "apm_modules").mkdir()
    (tmp_path / "apm_modules" / "bundler").symlink_to(
        tmp_path / "outside", target_is_directory=True
    )
    lock = LockFile(local_staged_files=[_STAGED], local_staged_file_hashes={_STAGED: _HASH})
    assert lock.staged_hash_drift(tmp_path) == []


def test_staged_hash_drift_skips_linked_ancestors_inside_modules(tmp_path: Path) -> None:
    """Compile discovery never follows a linked component, so drift makes no claim on it."""
    real = tmp_path / "apm_modules" / "real"
    real.mkdir(parents=True)
    (real / "glob.instructions.md").write_text("edited\n", encoding="ascii")
    (tmp_path / "apm_modules" / "bundler" / ".apm").mkdir(parents=True)
    (tmp_path / "apm_modules" / "bundler" / ".apm" / "instructions").symlink_to(
        real, target_is_directory=True
    )
    lock = LockFile(local_staged_files=[_STAGED], local_staged_file_hashes={_STAGED: _HASH})
    assert lock.staged_hash_drift(tmp_path) == []
