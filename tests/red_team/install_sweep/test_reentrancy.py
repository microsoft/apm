"""Vector 6 -- re-entrancy / exception safety of the new locks & kill path.

- An exception raised inside the `_trust_store_lock()` with-block must
  release BOTH the thread lock and the fcntl file lock (no deadlock on the
  next acquire) and must leave the store intact.
- `_kill_process_group` must swallow any OSError family raised by killpg /
  getpgid and never propagate into the install flow.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from apm_cli.core import script_trust as st
from apm_cli.core.script_executors import _kill_process_group

from .conftest import write_project


def test_exception_in_trust_lock_releases_and_preserves_store(
    apm_home: Path, tmp_path: Path
) -> None:
    """Raising inside the lock must not deadlock and must not corrupt store."""
    yml = write_project(tmp_path / "p", "post-install", ["echo hi"])
    st.trust_project_scripts(yml)
    before = st._load_trust_store()

    with pytest.raises(RuntimeError, match="boom"):
        with st._trust_store_lock():
            raise RuntimeError("boom")

    # Re-acquire must succeed promptly -- proves the lock was released.
    acquired = threading.Event()

    def _reacquire() -> None:
        with st._trust_store_lock():
            acquired.set()

    t = threading.Thread(target=_reacquire)
    t.start()
    t.join(timeout=5)
    assert acquired.is_set(), "trust-store lock not released after an exception (deadlock)"

    # The thread lock itself must be free (acquirable without blocking).
    assert st._TRUST_STORE_THREAD_LOCK.acquire(timeout=2), "thread lock leaked"
    st._TRUST_STORE_THREAD_LOCK.release()

    after = st._load_trust_store()
    assert after == before, "store mutated by an aborted lock section"


def test_normal_trust_still_works_after_aborted_section(apm_home: Path, tmp_path: Path) -> None:
    """A subsequent real trust() must still persist after an aborted section."""
    yml = write_project(tmp_path / "p", "post-install", ["echo hi"])
    with pytest.raises(ValueError):
        with st._trust_store_lock():
            raise ValueError("x")
    fp = st.trust_project_scripts(yml)
    assert fp is not None
    assert st.is_project_scripts_trusted(yml)


def test_kill_process_group_swallows_killpg_oserror(
    apm_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If killpg raises, _kill_process_group must not propagate it."""

    class _FakeProc:
        pid = 999999
        returncode = None

        def kill(self) -> None:  # fallback path
            raise OSError("kill also fails")

        def communicate(self, timeout=None):
            return ("", "")

    def _raise_getpgid(_pid):
        raise PermissionError("EPERM on getpgid")

    monkeypatch.setattr("apm_cli.core.script_executors.os.getpgid", _raise_getpgid)
    # Must not raise even though both killpg(getpgid) and proc.kill() fail.
    _kill_process_group(_FakeProc())  # type: ignore[arg-type]


def test_kill_process_group_handles_process_lookup(
    apm_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ProcessLookupError from killpg must be swallowed (already-dead group)."""

    class _FakeProc:
        pid = 999998
        returncode = 0

        def kill(self) -> None:
            pass

        def communicate(self, timeout=None):
            return ("", "")

    def _raise_killpg(_pgid, _sig):
        raise ProcessLookupError("no such group")

    monkeypatch.setattr("apm_cli.core.script_executors.os.getpgid", lambda _p: 12345)
    monkeypatch.setattr("apm_cli.core.script_executors.os.killpg", _raise_killpg)
    _kill_process_group(_FakeProc())  # type: ignore[arg-type]
