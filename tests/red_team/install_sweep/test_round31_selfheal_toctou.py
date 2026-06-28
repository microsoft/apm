"""Round 31 -- self-heal TOCTOU + log-dir symlink probes (expected CLEAN).

Domain: install / audit-evasion. Target surface (REAL):
``_append_to_script_log`` round-30 self-heal (unlink + O_EXCL|O_NOFOLLOW
recreate) and ``_get_scripts_log_path`` / dir handling. Driven directly.

These confirm the NEW (one-round-old) self-heal code resists the symlink swap,
does not follow out of ~/.apm, stays bounded under a hostile re-plant loop
(no CPU spin / unbounded retry), and does not regress the benign FIFO heal.
A deterministic CLEAN here is the intended result.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
import threading
import time
from pathlib import Path

from apm_cli.core.script_executors import (
    _append_to_script_log,
    _get_scripts_log_path,
)

_CEILING_S = 4.0


def _bounded_append(token: str, ceiling: float = _CEILING_S) -> float:
    done: dict[str, bool] = {}

    def _run() -> None:
        _append_to_script_log("post-install", "command", f"echo {token}", stdout=token)
        done["ok"] = True

    th = threading.Thread(target=_run, daemon=True)
    t0 = time.monotonic()
    th.start()
    th.join(ceiling)
    elapsed = time.monotonic() - t0
    assert not th.is_alive(), f"append for {token} blocked >{ceiling}s"
    assert done.get("ok") is True
    return elapsed


def _regular_0600(p: Path) -> bool:
    if not (p.exists() and not os.path.islink(p)):
        return False
    st = os.stat(p)
    return stat.S_ISREG(st.st_mode) and (st.st_mode & 0o777) == 0o600


def test_round31_symlink_at_logpath_self_heals_no_follow(apm_home):
    """A symlink planted at scripts.log -> victim outside ~/.apm:
    O_NOFOLLOW rejects it, the self-heal unlinks the LINK (not the target) and
    recreates a fresh 0600 regular file. Victim file is untouched. CLEAN.
    """
    log_path = _get_scripts_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if log_path.exists() or os.path.islink(log_path):
        log_path.unlink()

    victim_dir = Path(tempfile.mkdtemp())
    victim = victim_dir / "victim.txt"
    victim.write_text("ORIGINAL_VICTIM\n")
    os.symlink(victim, log_path)
    assert os.path.islink(log_path)

    elapsed = _bounded_append("AFTER_SYMLINK")
    assert elapsed < _CEILING_S

    assert victim.read_text().strip() == "ORIGINAL_VICTIM", (
        "self-heal must NOT follow the symlink and clobber the victim target"
    )
    assert not os.path.islink(log_path), "symlink must be replaced by a real file"
    assert _regular_0600(log_path), "self-healed log must be a 0600 regular file"
    assert "AFTER_SYMLINK" in log_path.read_text()


def test_round31_repeated_replant_is_bounded_no_spin(apm_home):
    """Hostile re-plant racing the self-heal must retry at most once then drop
    THIS write -- never CPU-spin or retry unboundedly.

    We deterministically force the worst case by monkeypatching the module's
    os.unlink so that every unlink the self-heal performs immediately RE-PLANTS
    a no-reader FIFO (the round-30 hostile node). The append must still return
    promptly (bounded) and must not loop. We count unlink calls to prove the
    retry is bounded (at most a small constant per append), not unbounded.
    """
    import apm_cli.core.script_executors as se

    log_path = _get_scripts_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if log_path.exists() or os.path.islink(log_path):
        log_path.unlink()
    os.mkfifo(log_path)  # initial hostile node -> ENXIO on O_NONBLOCK open

    real_unlink = os.unlink
    calls = {"n": 0}

    def _hostile_unlink(path, *a, **k):
        calls["n"] += 1
        real_unlink(path)
        if calls["n"] < 50:  # adversary keeps re-planting a fresh FIFO
            with contextlib.suppress(OSError):
                os.mkfifo(path)

    orig = se.os.unlink
    se.os.unlink = _hostile_unlink  # type: ignore[assignment]
    try:
        done: dict[str, bool] = {}

        def _run() -> None:
            _append_to_script_log("post-install", "command", "echo SPIN", stdout="x")
            done["ok"] = True

        th = threading.Thread(target=_run, daemon=True)
        t0 = time.monotonic()
        th.start()
        th.join(_CEILING_S)
        elapsed = time.monotonic() - t0
        assert not th.is_alive(), "append spun/wedged under a hostile re-plant loop"
        assert done.get("ok") is True
        # Bounded retry: a single append must not call unlink an unbounded number
        # of times. The self-heal does at most ~2 unlinks (ENXIO branch + the
        # non-regular fstat branch). Allow generous slack but cap it hard.
        assert calls["n"] <= 4, f"self-heal unlinked {calls['n']}x -- unbounded retry/spin"
        assert elapsed < _CEILING_S
    finally:
        se.os.unlink = orig  # type: ignore[assignment]
        with __import__("contextlib").suppress(OSError):
            if os.path.islink(log_path) or log_path.exists():
                real_unlink(log_path)


def test_round31_logdir_is_symlink_unlink_stays_scoped(apm_home):
    """If ~/.apm/logs is itself a symlink to an attacker dir, the FIXED log_path
    (logs/scripts.log) resolves through it, but unlink only ever removes the
    file literally named scripts.log inside that dir -- it cannot be steered to
    delete an arbitrary attacker-named victim. CLEAN (bounded blast radius).
    """
    base = Path(os.environ["APM_HOME"])
    real_logs = Path(tempfile.mkdtemp())
    # Pre-seed an unrelated victim file the attacker dir also contains.
    other = real_logs / "important_other.txt"
    other.write_text("KEEP_ME\n")
    # Plant logs/ as a symlink to the attacker-controlled dir.
    link = base / "logs"
    if link.exists() or os.path.islink(link):
        if os.path.islink(link):
            link.unlink()
    os.symlink(real_logs, link)

    # Plant a hostile FIFO at the resolved scripts.log to force the self-heal.
    log_path = _get_scripts_log_path()
    if os.path.islink(log_path) or log_path.exists():
        log_path.unlink()
    os.mkfifo(log_path)

    _bounded_append("THROUGH_DIR_SYMLINK")

    assert other.read_text().strip() == "KEEP_ME", (
        "self-heal unlink must not delete an unrelated file in the dir"
    )
    # scripts.log self-healed to a real 0600 file inside the (symlinked) dir.
    assert _regular_0600(log_path)
    assert "THROUGH_DIR_SYMLINK" in log_path.read_text()


def test_round31_benign_fifo_self_heal_regression_guard(apm_home):
    """Round-30 self-heal still works for the benign no-reader FIFO case:
    a FIFO planted at scripts.log self-heals to a recorded 0600 regular file.
    Guards against a regression introduced by the proposed mode fix.
    """
    log_path = _get_scripts_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.path.islink(log_path) or log_path.exists():
        log_path.unlink()
    os.mkfifo(log_path)

    elapsed = _bounded_append("FIFO_HEAL")
    assert elapsed < _CEILING_S
    assert _regular_0600(log_path), "FIFO must self-heal to a 0600 regular file"
    assert "FIFO_HEAL" in log_path.read_text()
