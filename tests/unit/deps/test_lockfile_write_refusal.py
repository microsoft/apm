"""The lockfile writer scope commits nothing when a check refusal or an interrupt ends it."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.lockfile_write_authority import (
    LockfileWriter,
    lockfile_write_scope,
    stage_lockfile,
)
from apm_cli.install.errors import PolicyViolationError

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-LAW-transaction.failed_command_preserves_state"),
]


class _Rendered(RuntimeError):
    """A command-level wrapper raised from the original failure."""


def _refusal() -> BaseException:
    try:
        raise PolicyViolationError("Install-time audit blocked")
    except PolicyViolationError as refusal:
        try:
            raise _Rendered("rendered") from refusal
        except _Rendered as wrapped:
            return wrapped


def _exit_after_refusal() -> BaseException:
    try:
        raise PolicyViolationError("blocked")
    except PolicyViolationError:
        try:
            raise SystemExit(1)
        except SystemExit as exit_:
            return exit_


@pytest.mark.parametrize(
    ("error", "committed"),
    [
        (PolicyViolationError("blocked"), False),
        (_refusal(), False),
        (RuntimeError("native write failed after deploy"), True),
        (SystemExit(1), True),
        (_exit_after_refusal(), False),
        (SystemExit(0), True),
        (KeyboardInterrupt(), False),
    ],
    ids=[
        "refusal",
        "refusal-in-cause-chain",
        "failure-after-deploy",
        "exit-1-after-deploy",
        "exit-1-after-refusal",
        "exit-0",
        "interrupt",
    ],
)
def test_scope_commits_unless_the_command_was_refused(
    tmp_path: Path, error: BaseException, committed: bool
) -> None:
    lock = tmp_path / "apm.lock.yaml"
    with pytest.raises(type(error)), lockfile_write_scope(LockfileWriter.INSTALL):
        stage_lockfile(lock, "lockfile_version: '1'\n")
        raise error

    assert lock.exists() is committed
