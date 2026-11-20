"""User-scope local-bundle rows keep ``scope: user`` across ledger rebuilds (#3995) (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = pytest.mark.component

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


def test_round_trip_through_the_lock_file_keeps_user_scope(tmp_path) -> None:
    path = tmp_path / "apm.lock.yaml"
    _user_bundle_lock().write(path)
    lockfile = LockFile.read(path)
    lockfile.dependencies["owner/other"] = LockedDependency(repo_url="owner/other")
    DeploymentLedgerCodec.replace_legacy_owner(
        lockfile, "owner/other", [".claude/skills/other/SKILL.md"], {}
    )
    lockfile.write(path)
    (row,) = _bundle_rows(LockFile.read(path))
    assert row["scope"] == "user"
