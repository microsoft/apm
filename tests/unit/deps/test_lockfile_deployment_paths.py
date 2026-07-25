"""Tests for lockfile-owned deployment path mutations."""

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile


def test_rename_local_deployed_path_moves_path_and_hash_without_duplicates() -> None:
    lockfile = LockFile(
        local_deployed_files=["old.md", "new.md", "old.md"],
        local_deployed_file_hashes={"old.md": "sha256:old"},
    )

    lockfile.rename_local_deployed_path("old.md", "new.md")

    assert lockfile.local_deployed_files == ["new.md"]
    assert lockfile.local_deployed_file_hashes == {"new.md": "sha256:old"}


def test_rename_local_deployed_path_is_noop_when_old_path_is_absent() -> None:
    lockfile = LockFile(
        local_deployed_files=["kept.md"],
        local_deployed_file_hashes={"old.md": "sha256:orphan"},
    )

    lockfile.rename_local_deployed_path("missing.md", "new.md")

    assert lockfile.local_deployed_files == ["kept.md"]
    assert lockfile.local_deployed_file_hashes == {"old.md": "sha256:orphan"}


def test_rename_local_deployed_path_invalidates_canonical_projection() -> None:
    lockfile = LockFile(
        local_deployed_files=["old.md"],
        local_deployed_file_hashes={"old.md": "sha256:old"},
    )
    lockfile.deployment_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    lockfile._deployments_present = True

    lockfile.rename_local_deployed_path("old.md", "new.md")

    assert lockfile.deployment_ledger.records == {}
    assert lockfile._deployments_present is False
