"""Round-26 red-team: install-path process/concurrency + round-25 rotation lock.

Domain: subprocess lifecycle, output-capture bounds, process-group reaping,
log + trust-store concurrency, and the round-25 fcntl log-rotation lock.

All assertions exercise the REAL helpers in
``apm_cli.core.script_executors`` and ``apm_cli.core.script_trust`` with
real files / real subprocesses. macOS-strict harness: no kill/pkill/timeout;
hangs are bounded with daemon-thread watchdogs; liveness probed with signal 0.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from apm_cli.core import script_executors as se
from apm_cli.core import script_trust as st

_WORKERS = Path(__file__).resolve().parent / "_workers"


def _run_bounded(fn, budget: float):
    """Run ``fn`` on a daemon thread; return (completed, elapsed)."""
    done = threading.Event()
    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["r"] = fn()
        except BaseException as exc:
            box["exc"] = exc
        finally:
            done.set()

    t = threading.Thread(target=_target, daemon=True)
    start = time.monotonic()
    t.start()
    completed = done.wait(timeout=budget)
    return completed, time.monotonic() - start, box


# --------------------------------------------------------------------------
# CLEAN CONFIRM: round-25 log rotation lock is O_NOFOLLOW-safe
# --------------------------------------------------------------------------


def test_round25_log_lock_symlink_target_untouched(tmp_path, monkeypatch):
    """Pre-plant scripts.log.lock as a symlink to a sentinel; rotation must
    NOT follow it (O_NOFOLLOW) and the sentinel must be byte-intact."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    log_path = logs / "scripts.log"
    # Force over-cap so rotation is actually attempted.
    log_path.write_bytes(b"A" * (se._MAX_LOG_BYTES + 16))

    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("PRECIOUS-AUDIT-DATA")
    (logs / "scripts.log.lock").symlink_to(sentinel)

    se._rotate_log_if_large(log_path)

    assert sentinel.read_text() == "PRECIOUS-AUDIT-DATA", "log lock followed symlink"


def test_round25_rotated_dest_symlink_target_untouched(tmp_path, monkeypatch):
    """Pre-plant scripts.log.1 as a symlink; os.replace must rename the
    symlink itself, never truncate/clobber its target."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    log_path = logs / "scripts.log"
    log_path.write_bytes(b"B" * (se._MAX_LOG_BYTES + 16))

    sentinel = tmp_path / "victim.txt"
    sentinel.write_text("DO-NOT-CLOBBER")
    (logs / "scripts.log.1").symlink_to(sentinel)

    se._rotate_log_if_large(log_path)

    assert sentinel.read_text() == "DO-NOT-CLOBBER", "rename followed dest symlink"


# --------------------------------------------------------------------------
# GENUINE BREAK: trust-store lock follows symlink + O_TRUNC -> clobber
# --------------------------------------------------------------------------


def test_round26_trust_lock_symlink_clobbers_target(tmp_path, monkeypatch):
    """The trust-store lock opens ``<store>.lock`` with plain open(,"w") --
    no O_NOFOLLOW and O_TRUNC. A pre-planted symlink lock is FOLLOWED and the
    target is truncated to zero bytes. Unlike the round-25 log lock, the trust
    lock was never hardened. SECURE EXPECTATION: target byte-intact."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    (tmp_path).mkdir(parents=True, exist_ok=True)

    sentinel = tmp_path / "innocent_user_file.txt"
    sentinel.write_text("USER-DATA-MUST-SURVIVE")

    store = st._trust_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    lock_path = store.with_name(store.name + ".lock")
    lock_path.symlink_to(sentinel)

    apm = tmp_path / "proj" / "apm.yml"
    apm.parent.mkdir(parents=True)
    apm.write_text("lifecycle:\n  install:\n    - run: echo hi\n")

    # Real trust write -> enters _trust_store_lock() -> open(lock,"w").
    st.trust_project_scripts(apm)

    assert sentinel.read_text() == "USER-DATA-MUST-SURVIVE", (
        "trust lock followed symlink and truncated the target file"
    )


def test_round26_trust_lock_symlink_wipes_existing_trust(tmp_path, monkeypatch):
    """Stronger: point the trust lock symlink at the trust store ITSELF.
    The lock's O_TRUNC truncates the store before it is read, so the next
    trust write loads an EMPTY store and silently drops every previously
    trusted project. SECURE EXPECTATION: project A stays trusted."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))

    proj_a = tmp_path / "a" / "apm.yml"
    proj_a.parent.mkdir(parents=True)
    proj_a.write_text("lifecycle:\n  install:\n    - run: echo a\n")
    st.trust_project_scripts(proj_a)
    assert st.is_project_scripts_trusted(proj_a), "precondition: A trusted"

    store = st._trust_store_path()
    lock_path = store.with_name(store.name + ".lock")
    # Attacker with write access to ~/.apm/ removes the real lock file left by
    # A's trust write and redirects the predictable lock path at the store
    # itself; a plain open(lock, "w") would O_TRUNC the store before it is read.
    if lock_path.exists() or lock_path.is_symlink():
        lock_path.unlink()
    lock_path.symlink_to(store)

    proj_b = tmp_path / "b" / "apm.yml"
    proj_b.parent.mkdir(parents=True)
    proj_b.write_text("lifecycle:\n  install:\n    - run: echo b\n")
    st.trust_project_scripts(proj_b)

    assert st.is_project_scripts_trusted(proj_a), (
        "trust lock truncation wiped project A's trust record (lost-update)"
    )


# --------------------------------------------------------------------------
# round-25 rotation lock: blocking flock contention (install-hang surface)
# --------------------------------------------------------------------------


def test_round25_rotation_blocking_flock_unbounded(tmp_path, monkeypatch):
    """fcntl.flock at line 679 is LOCK_EX with no LOCK_NB and no timeout.
    A process holding the dedicated lock starves any concurrent rotator
    INDEFINITELY -> ``apm install`` hangs for as long as the holder lives.
    Proof: a real subprocess holds the lock and sleeps well past the budget;
    a watchdog shows the in-process rotation does NOT complete in time."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    log_path = logs / "scripts.log"
    log_path.write_bytes(b"C" * (se._MAX_LOG_BYTES + 16))
    lock_path = logs / "scripts.log.lock"
    ready = tmp_path / "ready.flag"

    hold_s = 30.0
    holder = subprocess.Popen(
        [
            sys.executable,
            str(_WORKERS / "flock_holder.py"),
            str(lock_path),
            str(hold_s),
            str(ready),
        ],
    )
    try:
        # Wait until the holder confirms it owns the lock.
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), "holder never acquired the lock"

        budget = 4.0
        completed, elapsed, _ = _run_bounded(lambda: se._rotate_log_if_large(log_path), budget)
        # SECURE EXPECTATION: rotation should not block install indefinitely on
        # a foreign lock holder. It DOES (blocking LOCK_EX) -> watchdog times out.
        assert completed, (
            f"rotation blocked on a foreign lock holder for >{budget}s "
            f"(elapsed {elapsed:.1f}s) -- install-hang DoS via predictable lock path"
        )
    finally:
        holder.wait()


# --------------------------------------------------------------------------
# CLEAN CONFIRM: concurrent rotation does not lose records (round-25 fix)
# --------------------------------------------------------------------------


def _append_worker(apm_home: str, marker: str) -> None:
    os.environ["APM_HOME"] = apm_home
    # Re-import fresh in the child so env is honoured.
    from apm_cli.core import script_executors as child_se

    child_se._append_to_script_log("install", "command", marker, status="ok")


def test_round25_concurrent_rotation_no_record_loss(tmp_path, monkeypatch):
    """Many processes append unique markers while the log sits over the
    rotation threshold. Records may move into scripts.log.1 but NONE may be
    destroyed below the two-file retention. Confirms the round-25 lock."""
    monkeypatch.setenv("APM_HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    log_path = logs / "scripts.log"
    log_path.write_bytes(b"D" * (se._MAX_LOG_BYTES + 16))

    markers = [f"MARKER-{i:04d}-ZZ" for i in range(24)]
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_append_worker, args=(str(tmp_path), m)) for m in markers]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0, "append worker crashed"

    combined = ""
    for name in ("scripts.log", "scripts.log.1"):
        f = logs / name
        if f.exists():
            combined += f.read_text(errors="replace")
    missing = [m for m in markers if m not in combined]
    assert not missing, f"records destroyed by concurrent rotation: {missing}"
