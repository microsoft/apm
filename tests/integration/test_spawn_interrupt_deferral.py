"""Interrupt deferral around a session-isolated spawn (#5303).

A lifecycle command script runs in its own session, so only APM can stop it,
and Popen returns only after the shell has exec'd. ``cli.interrupts_deferred``
holds Ctrl+C and SIGTERM across that spawn so the script always has a handle
to reap, while keeping the caller's SIGTERM policy.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.core.lifecycle_scripts import LifecycleEvent, PackageInfo, ScriptEntry
from apm_cli.core.script_executors import _execute_command

pytestmark = [
    pytest.mark.integration,
    pytest.mark.component,
    pytest.mark.promise("P-OWN-process-interrupt-signals"),
]


def _make_event(event_name: str) -> LifecycleEvent:
    return LifecycleEvent(
        event=event_name,
        packages=[PackageInfo(name="org/repo", reference="v1")],
        scope="project",
        timestamp="2026-01-01T00:00:00Z",
        cli_version="0.0.0",
        working_directory="/tmp/test",
    )


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups")
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_interrupt_while_spawning_still_reaps_the_script(tmp_path: Path, signum: int) -> None:
    """An interrupt that lands inside Popen, after the script started, still reaps it (#5303)."""
    from apm_cli.cli import _interrupt_on_sigterm

    started = tmp_path / "started.txt"
    script = ScriptEntry(
        script_type="command",
        event="pre-compile",
        bash=f"echo $$ > '{started}'; sleep 60",
        timeout_sec=60,
    )
    real_popen = subprocess.Popen

    def _popen_interrupted_after_exec(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        deadline = time.monotonic() + 10
        while not (started.exists() and started.read_text(encoding="ascii").strip()):
            assert time.monotonic() < deadline, "script never started"
            time.sleep(0.02)
        signal.raise_signal(signum)
        return proc

    saved = signal.signal(signal.SIGTERM, _interrupt_on_sigterm)
    try:
        with (
            patch.object(subprocess, "Popen", side_effect=_popen_interrupted_after_exec),
            pytest.raises(KeyboardInterrupt),
        ):
            _execute_command(script, _make_event("pre-compile"), project_root=str(tmp_path))
    finally:
        signal.signal(signal.SIGTERM, saved)

    shell = int(started.read_text(encoding="ascii"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(shell, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.killpg(shell, signal.SIGKILL)
    pytest.fail("a script spawned as the interrupt landed outlived the APM command")


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX signals")
@pytest.mark.parametrize(
    ("policy", "returncode"),
    [("_interrupt_on_sigterm", -signal.SIGTERM), ("signal.SIG_IGN", 3)],
    ids=["cli-policy-terminates", "ignored-stays-ignored"],
)
def test_two_sigterms_while_spawning_keep_the_callers_policy(policy: str, returncode: int) -> None:
    """Deferral keeps the SIGTERM policy: the CLI's second one terminates, SIG_IGN ignores."""
    code = (
        "import os, signal, sys, time\n"
        "from apm_cli.cli import _interrupt_on_sigterm, interrupts_deferred\n"
        f"signal.signal(signal.SIGTERM, {policy})\n"
        "with interrupts_deferred():\n"
        "    os.kill(os.getpid(), signal.SIGTERM)\n"
        "    os.kill(os.getpid(), signal.SIGTERM)\n"
        "    time.sleep(1)\n"
        "sys.exit(3)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == returncode, (result.returncode, result.stderr)


def test_an_interrupt_during_handler_restore_still_restores_every_handler() -> None:
    """A KeyboardInterrupt raised midway through restoring leaves no handler on the deferral."""
    from apm_cli import cli

    saved = {num: signal.getsignal(num) for num in (signal.SIGINT, signal.SIGTERM)}
    signal.signal(signal.SIGTERM, cli._interrupt_on_sigterm)
    real_signal = signal.signal
    raised: list[int] = []

    def _interrupt_first_restore(num, handler):
        previous = real_signal(num, handler)
        if getattr(handler, "__name__", "") != "_defer" and not raised:
            raised.append(num)
            raise KeyboardInterrupt
        return previous

    try:
        with (
            patch.object(signal, "signal", side_effect=_interrupt_first_restore),
            pytest.raises(KeyboardInterrupt),
            cli.interrupts_deferred(),
        ):
            pass
        assert raised, "restore never ran"
        assert signal.getsignal(signal.SIGTERM) is cli._interrupt_on_sigterm
        assert signal.getsignal(signal.SIGINT) is saved[signal.SIGINT]
    finally:
        for num, handler in saved.items():
            if handler is not None:
                signal.signal(num, handler)
