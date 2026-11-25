"""The macOS branch of the journal ACL check, driven through a fake libc (#5178).

Python has no ``os.listxattr`` on macOS, so the check reads the extended ACL
through libc (``acl_get_link_np``). These cells replace that binding, so the
branch runs on every platform's CI.
"""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-LAW-transaction.failed_command_preserves_state"),
    pytest.mark.skipif(os.name == "nt", reason="Windows takes the DACL branch first"),
]

_ACL = 0x5178  # an opaque acl_t handle
_ACL_TYPE_EXTENDED = 0x100


class _FakeLibc:
    """``acl_get_link_np`` / ``acl_get_entry`` / ``acl_free`` with a scripted outcome."""

    def __init__(self, acl: int | None, error: int = 0, entry: int = 0) -> None:
        self.acl, self.error, self.entry = acl, error, entry
        self.opened: list[tuple[bytes, int]] = []
        self.freed: list[int] = []

    def acl_get_link_np(self, path: bytes, kind: int) -> int | None:
        self.opened.append((path, kind))
        if self.acl is None:
            ctypes.set_errno(self.error)
        return self.acl

    def acl_get_entry(self, acl: int, which: int, entry) -> int:
        if self.entry:
            ctypes.set_errno(errno.EINVAL)
        return self.entry

    def acl_free(self, acl: int) -> int:
        self.freed.append(acl)
        return 0


@pytest.mark.parametrize(
    ("libc", "carries"),
    [
        pytest.param(_FakeLibc(_ACL), True, id="an-entry"),
        pytest.param(_FakeLibc(None, errno.ENOENT), False, id="no-acl"),
        pytest.param(_FakeLibc(_ACL, entry=-1), False, id="an-empty-acl"),
        pytest.param(_FakeLibc(None, errno.EACCES), True, id="unreadable-fails-closed"),
    ],
)
def test_the_darwin_branch_reads_the_extended_acl_without_following_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, libc: _FakeLibc, carries: bool
) -> None:
    from apm_cli.install import journal_modes

    target = tmp_path / "SKILL.md"
    target.write_bytes(b"x\n")
    monkeypatch.setattr(journal_modes, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(journal_modes, "_macos_acl_api", lambda: libc)

    assert journal_modes.carries_acl(target) is carries
    assert libc.opened == [(os.fsencode(target), _ACL_TYPE_EXTENDED)]
    assert libc.freed == ([] if libc.acl is None else [_ACL])
