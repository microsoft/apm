"""SIGTERM reaches APM commands as an interrupt, like Ctrl+C (#3676)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap

import pytest

pytestmark = [pytest.mark.skipif(os.name == "nt", reason="POSIX signals"), pytest.mark.unit]

_PROBE = textwrap.dedent(
    """
    import os, signal, sys, time
    import apm_cli.cli as entry

    def fake_cli(obj, **_kwargs):
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


_EXPAND_PROBE = textwrap.dedent(
    """
    import apm_cli.cli as entry

    def recording_cli(**kwargs):
        print(sorted(kwargs.items()))

    entry.cli = recording_cli
    entry.main()
    """
)


def test_main_does_not_glob_expand_arguments_on_windows() -> None:
    """`--skill '*'` must reach APM as `*` on Windows too (#4407)."""
    probe = subprocess.run(
        (sys.executable, "-c", _EXPAND_PROBE),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert probe.stdout.strip() == "[('obj', {}), ('windows_expand_args', False)]"
