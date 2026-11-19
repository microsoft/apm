"""SIGTERM reaches APM commands as an interrupt, like Ctrl+C (#3676)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX signals")

_PROBE = textwrap.dedent(
    """
    import os, signal, sys, time
    import apm_cli.cli as entry

    def fake_cli(obj):
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
        except KeyboardInterrupt:
            print("interrupted", signal.getsignal(signal.SIGTERM) == signal.SIG_DFL)
            sys.stdout.flush()
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
        print("not interrupted")

    entry.cli = fake_cli
    entry.main()
    """
)


def test_sigterm_interrupts_once_then_terminates() -> None:
    probe = subprocess.run(
        (sys.executable, "-c", _PROBE),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert probe.stdout.strip() == "interrupted True", probe.stdout + probe.stderr
    assert probe.returncode == -signal.SIGTERM
