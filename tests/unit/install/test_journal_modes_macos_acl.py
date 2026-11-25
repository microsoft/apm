"""The macOS branch of the journal ACL check, driven through a fake libc (#5178).

Python has no ``os.listxattr`` on macOS, so the check reads the extended ACL
through libc (``acl_get_link_np``). These cells replace the libc-loading
boundary (``ctypes.CDLL``), so the branch runs on every platform's CI. The
checked path is never created: the fake libc alone decides.
"""

from __future__ import annotations

import ctypes
import ctypes.util  # loaded before sys.platform is patched
import errno
import os
import sys
from collections.abc import Iterator
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
_TARGET = Path("/apm-5178-never-created/SKILL.md")


def _fake_libc(acl: int | None, error: int = 0, entry: int = 0) -> SimpleNamespace:
    """libc's ``acl_*`` calls with a scripted outcome; they accept signature attributes."""
    calls = SimpleNamespace(opened=[], freed=[])

    def acl_get_link_np(path: bytes, kind: int) -> int | None:
        calls.opened.append((path, kind))
        if acl is None:
            ctypes.set_errno(error)
        return acl

    def acl_get_entry(handle: int, which: int, out) -> int:
        if entry:
            ctypes.set_errno(errno.EINVAL)
        return entry

    def acl_free(handle: int) -> int:
        calls.freed.append(handle)
        return 0

    return SimpleNamespace(
        calls=calls,
        acl_get_link_np=acl_get_link_np,
        acl_get_entry=acl_get_entry,
        acl_free=acl_free,
    )


@pytest.fixture
def darwin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Select the darwin branch, with the cached libc binding cleared around the cell."""
    from apm_cli.install import journal_modes

    journal_modes._macos_acl_api.cache_clear()
    monkeypatch.setattr(sys, "platform", "darwin")
    yield
    monkeypatch.undo()
    journal_modes._macos_acl_api.cache_clear()


@pytest.mark.parametrize(
    ("outcome", "carries"),
    [
        pytest.param({"acl": _ACL}, True, id="an-entry"),
        pytest.param({"acl": None, "error": errno.ENOENT}, False, id="no-acl"),
        pytest.param({"acl": _ACL, "entry": -1}, False, id="an-empty-acl"),
        pytest.param({"acl": None, "error": errno.EACCES}, True, id="unreadable-fails-closed"),
    ],
)
def test_the_darwin_branch_reads_the_extended_acl_without_following_links(
    monkeypatch: pytest.MonkeyPatch, darwin, outcome: dict, carries: bool
) -> None:
    from apm_cli.install.journal_modes import carries_acl

    target = _TARGET
    libc = _fake_libc(**outcome)
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: libc)  # the libc boundary

    assert carries_acl(target) is carries
    assert libc.calls.opened == [(os.fsencode(target), _ACL_TYPE_EXTENDED)]
    assert libc.calls.freed == ([] if outcome["acl"] is None else [_ACL])


def test_a_broken_libc_binding_fails_closed(monkeypatch: pytest.MonkeyPatch, darwin) -> None:
    """A binding that raises a non-OSError (a missing symbol) keeps the path, never crashes."""
    from apm_cli.install.journal_modes import carries_acl

    target = _TARGET
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())

    assert carries_acl(target) is True
