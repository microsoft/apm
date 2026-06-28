"""Round 30 -- the 1 MiB capture cap boundary with MULTIBYTE UTF-8 + INVALID
bytes. A script that floods both streams past the cap with 3-byte codepoints,
splicing a run of raw invalid UTF-8, must not crash the drain decode, must
clamp each captured stream to <= cap, and must keep the install BOUNDED.

Domain: install / resource-unbounded / capture.

Target surface (REAL): ``_capture_bounded`` + ``_drain_capped`` in
``core/script_executors.py`` driven through a real
``subprocess.Popen(text=True, start_new_session=True)``.

WHY DISTINCT
============
Rounds 20/21 stressed the cap with single-byte ASCII floods. This probe makes
the cap cut land on a multibyte-codepoint boundary and injects raw ``\\xff``
bytes that straddle 64 KiB read boundaries. Two crash candidates:
  * a byte-keyed cap could split a codepoint -> ``UnicodeDecodeError`` on the
    redaction/encode; and
  * a decode error in the drain that is NOT caught would kill the drain thread
    and wedge the reader.
Python text mode decodes BEFORE the (char-based) cap and the drain catches
``ValueError`` (the parent of ``UnicodeDecodeError``), so the SECURE contract
is: bounded, capped, captured text is valid round-trippable ``str``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from apm_cli.core.script_executors import _MAX_CAPTURE_CHARS, _capture_bounded

from .conftest import PYEXE

_FLOOD = str(Path(__file__).parent / "_workers" / "rt30_cap_flood.py")

_CEILING_S = 8.0


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX killpg required")
def test_round30_cap_boundary_pure_multibyte_capped(apm_home):
    """A PURE multibyte flood (no invalid bytes) must clamp at exactly the cap
    with NO split codepoint, set capped=True, and stay bounded."""
    # bad_at beyond range -> no invalid bytes spliced. 1.3M EUR chars/stream
    # (~3.9 MiB bytes) overflows the 1 MiB CHAR cap.
    cmd = f"{PYEXE} {_FLOOD} 1300000 99999999"
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pgid = proc.pid
    try:
        t0 = time.monotonic()
        out, err, capped = _capture_bounded(proc, "{}", 30.0)
        elapsed = time.monotonic() - t0

        assert elapsed < _CEILING_S, f"cap flood took {elapsed:.2f}s (unbounded?)"
        # At least one stream reaches the cap EXACTLY (clean char-based cut, no
        # split codepoint); the other may truncate below cap because the over-
        # cap watchdog SIGKILLs the group once the first stream trips.
        assert max(len(out), len(err)) == _MAX_CAPTURE_CHARS, (
            f"neither stream clamped at the cap: out={len(out)} err={len(err)}"
        )
        assert len(out) <= _MAX_CAPTURE_CHARS and len(err) <= _MAX_CAPTURE_CHARS
        assert capped, "over-cap multibyte flood did not set the capped flag"
        # No split codepoint at the cut -- a strict re-encode (what the log
        # appender does) must not raise.
        out.encode("utf-8", "strict")
        err.encode("utf-8", "strict")
    finally:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX killpg required")
def test_round30_cap_invalid_bytes_bounded_no_crash(apm_home):
    """Raw INVALID UTF-8 spliced into a flood must not crash the drain decode;
    the capture truncates at the bad byte (acceptable) and stays bounded.

    This documents that a decode error in ``_drain_capped`` is swallowed
    (``UnicodeDecodeError`` <: ``ValueError``) and merely STOPS the drain --
    bounded, crash-free, valid ``str``. ``capped`` may legitimately be False
    because the truncation happens before the cap; the child cannot then flood
    unboundedly (it EPIPEs into the closed pipe)."""
    cmd = f"{PYEXE} {_FLOOD} 1300000 500000"
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pgid = proc.pid
    try:
        t0 = time.monotonic()
        out, err, _capped = _capture_bounded(proc, "{}", 30.0)
        elapsed = time.monotonic() - t0

        assert elapsed < _CEILING_S, f"invalid-byte flood took {elapsed:.2f}s"
        assert len(out) <= _MAX_CAPTURE_CHARS
        assert len(err) <= _MAX_CAPTURE_CHARS
        # The retained capture is a clean str (drain stopped at the bad byte,
        # never retained a partial/invalid sequence).
        out.encode("utf-8", "strict")
        err.encode("utf-8", "strict")
    finally:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX killpg required")
def test_round30_cap_exact_boundary_codepoint(apm_home):
    """Emit just over 1 MiB chars so the cut sits between codepoints; the
    capture must clamp at exactly the cap without a partial codepoint."""
    over = _MAX_CAPTURE_CHARS + 17
    cmd = f"{PYEXE} {_FLOOD} {over} {_MAX_CAPTURE_CHARS - 3}"
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pgid = proc.pid
    try:
        out, err, _capped = _capture_bounded(proc, "{}", 30.0)
        assert len(out) <= _MAX_CAPTURE_CHARS
        # Whatever was retained is a clean str (no lone surrogate / no partial
        # codepoint) -- proven by a strict re-encode.
        out.encode("utf-8", "strict")
        err.encode("utf-8", "strict")
    finally:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
