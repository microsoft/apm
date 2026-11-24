"""A special file at a locked path is reported, never hidden as managed (#5277 review)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.policy.policy_checks import _check_unmanaged_files
from apm_cli.policy.schema import UnmanagedFilesPolicy

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I3")]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
@pytest.mark.parametrize(
    "deployed", [".claude/commands/run.md", ".claude/commands/"], ids=["file-row", "dir-row"]
)
def test_a_fifo_replacing_a_locked_file_is_unmanaged(tmp_path: Path, deployed: str) -> None:
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    os.mkfifo(tmp_path / ".claude" / "commands" / "run.md")
    lock = LockFile()
    lock.add_dependency(
        LockedDependency.from_dict({"repo_url": "org/pkg", "deployed_files": [deployed]})
    )
    policy = UnmanagedFilesPolicy(action="deny", directories=[".claude/commands"])

    result = _check_unmanaged_files(tmp_path, lock, policy)

    assert not result.passed
    assert any(".claude/commands/run.md" in line for line in result.details)
