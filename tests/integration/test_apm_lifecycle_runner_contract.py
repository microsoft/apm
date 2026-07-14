from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult


def test_default_runner_executes_real_apm_subprocess(tmp_path: Path) -> None:
    result = ApmLifecycleRunner().run(
        ("--help",),
        cwd=tmp_path,
        env=os.environ,
    )

    assert result.command == (sys.executable, "-m", "apm_cli.cli", "--help")
    assert result.returncode == 0
    assert "Usage: python -m apm_cli.cli [OPTIONS] COMMAND [ARGS]..." in result.stdout
    assert "Agent Package Manager (APM)" in result.stdout
    assert result.cwd == tmp_path


def test_runner_passes_explicit_cwd_and_environment(tmp_path: Path) -> None:
    parent_cwd = Path.cwd()
    parent_environment = dict(os.environ)
    runner = ApmLifecycleRunner(
        (
            sys.executable,
            "-c",
            (
                "import os, pathlib, sys; "
                "pathlib.Path('observed.txt').write_text(os.environ['OBSERVED']); "
                "print('captured stdout'); "
                "print('captured stderr', file=sys.stderr)"
            ),
        )
    )

    result = runner.run(
        (),
        cwd=tmp_path,
        env={**parent_environment, "OBSERVED": "isolated"},
    )

    assert result.returncode == 0
    assert result.stdout == "captured stdout\n"
    assert result.stderr == "captured stderr\n"
    assert (tmp_path / "observed.txt").read_text(encoding="utf-8") == "isolated"
    assert Path.cwd() == parent_cwd
    assert dict(os.environ) == parent_environment


def test_runner_raises_when_configured_timeout_expires(tmp_path: Path) -> None:
    command = (sys.executable, "-c", "import time; time.sleep(10)")
    runner = ApmLifecycleRunner(command, timeout_seconds=0.05)

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        runner.run((), cwd=tmp_path, env=os.environ)

    assert exc_info.value.cmd == command
    assert exc_info.value.timeout == 0.05


def test_run_sequence_preserves_order_and_results(tmp_path: Path) -> None:
    parent_cwd = Path.cwd()
    parent_environment = dict(os.environ)
    runner = ApmLifecycleRunner(
        (
            sys.executable,
            "-c",
            (
                "import pathlib, sys; "
                "pathlib.Path('sequence.txt').open('a').write(sys.argv[1]); "
                "sys.exit(int(sys.argv[2]))"
            ),
        )
    )

    commands = (("A", "0"), ("B", "7"), ("C", "0"))
    results = runner.run_sequence(
        commands,
        cwd=tmp_path,
        env=parent_environment,
    )

    assert [result.returncode for result in results] == [0, 7, 0]
    assert [result.command[-2:] for result in results] == list(commands)
    assert (tmp_path / "sequence.txt").read_text(encoding="utf-8") == "ABC"
    assert Path.cwd() == parent_cwd
    assert dict(os.environ) == parent_environment


def test_command_result_is_immutable(tmp_path: Path) -> None:
    result = CommandResult(
        command=("command",),
        returncode=0,
        stdout="stdout",
        stderr="stderr",
        cwd=tmp_path,
    )

    with pytest.raises(FrozenInstanceError):
        result.returncode = 1
