"""Lockfile writes reject staging paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.staging_guard import (
    STAGING_DIR_NAME,
    StagingPathLeakError,
)

pytestmark = pytest.mark.component

_STAGED_SCRIPT = f"/home/dev/.apm/apm_modules/{STAGING_DIR_NAME}/abc123/replacements/de/start.mjs"
_REFUSAL = "references the resolution staging directory"


def test_lockfile_write_rejects_staging_paths(tmp_path: Path) -> None:
    lock = LockFile()
    lock.mcp_servers = ["toolsrv"]
    lock.mcp_configs = {"toolsrv": {"command": "node", "args": [_STAGED_SCRIPT]}}
    lockfile_path = tmp_path / "apm.lock.yaml"

    with pytest.raises(StagingPathLeakError, match=_REFUSAL):
        lock.write(lockfile_path)

    assert not lockfile_path.exists()
