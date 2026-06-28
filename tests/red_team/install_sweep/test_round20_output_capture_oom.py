"""Round 20 -- unbounded in-memory stdout/stderr capture (OOM the installer).

`_MAX_LOG_FIELD_CHARS` (4096) and `_MAX_LOG_BYTES` (5 MiB) bound only the
*audit log file*. But `_execute_command` calls
`proc.communicate(input=..., timeout=...)`, which reads the ENTIRE child
stdout/stderr into Python objects BEFORE any truncation runs. A hostile or
runaway lifecycle script that prints a multi-GB blob to stdout is fully
buffered in the installer's address space -> OOM kills `apm install`. Log
truncation happens only after the whole blob is already resident.

These probes drive the REAL `_execute_command` and measure peak RSS of the
installer process as a function of the child's stdout size. Bounded capture
(the secure contract) would keep RSS flat regardless of how much the child
prints. The real code grows ~2 bytes of peak RSS per byte printed (raw byte
buffer + decoded str), proving there is no max-capture cap.
"""

from __future__ import annotations

import resource
import sys
from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import LifecycleEvent
from apm_cli.core.script_executors import _execute_command

from .conftest import make_command_entry

PYEXE = sys.executable
_EMIT = str(Path(__file__).parent / "_workers" / "emit.py")


def _peak_rss_bytes() -> int:
    """Peak RSS high-water mark for this process, normalised to bytes."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(rss) if sys.platform == "darwin" else int(rss) * 1024


def _emit_cmd(nbytes: int) -> str:
    """Command that drains stdin then prints *nbytes* of 'A' to stdout."""
    return f"{PYEXE} {_EMIT} {nbytes}"


@pytest.mark.slow
def test_stdout_capture_is_unbounded_in_memory(apm_home: Path) -> None:
    """Peak RSS scales with child stdout size -> no in-memory capture cap.

    A 1 MiB warm-up folds in interpreter/import overhead and primes the
    high-water mark. A 300 MiB run must NOT inflate peak RSS if capture were
    bounded. The real executor buffers the whole blob, so peak jumps by
    hundreds of MiB and this assert (the secure contract) FAILS -- proving
    the OOM vector.
    """
    ev = LifecycleEvent.create("post-install")

    _execute_command(make_command_entry(_emit_cmd(1 << 20), timeout_sec=180), ev)
    rss_before = _peak_rss_bytes()

    _execute_command(make_command_entry(_emit_cmd(300 << 20), timeout_sec=180), ev)
    rss_after = _peak_rss_bytes()

    delta_mib = (rss_after - rss_before) / (1 << 20)

    assert (rss_after - rss_before) < (64 << 20), (
        f"Installer peak RSS grew {delta_mib:.0f} MiB while a script printed "
        f"300 MiB to stdout: communicate() buffers the full output in memory "
        f"with no max-capture cap. A multi-GB stdout OOMs `apm install`; the "
        f"4096-char log truncation runs only AFTER the blob is resident."
    )
