from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult


def test_default_runner_executes_apm_console_script(tmp_path: Path) -> None:
    result = ApmLifecycleRunner().run(
        ("--help",),
        cwd=tmp_path,
        env=os.environ,
    )

    assert result.command == ("apm", "--help")
    assert result.returncode == 0
    assert "Usage: apm [OPTIONS] COMMAND [ARGS]..." in result.stdout
    assert "Agent Package Manager (APM)" in result.stdout
    assert result.cwd == tmp_path


def test_explicit_command_can_execute_source_module(tmp_path: Path) -> None:
    runner = ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli"))

    result = runner.run(("--help",), cwd=tmp_path, env=os.environ)

    assert result.command == (sys.executable, "-m", "apm_cli.cli", "--help")
    assert result.returncode == 0
    assert "Usage: python -m apm_cli.cli [OPTIONS] COMMAND [ARGS]..." in result.stdout


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
        expected_returncodes=(0, 7, 0),
        scenario_id="ordered-sequence",
        cwd=tmp_path,
        env=parent_environment,
    )

    assert [result.returncode for result in results] == [0, 7, 0]
    assert [result.command[-2:] for result in results] == list(commands)
    assert (tmp_path / "sequence.txt").read_text(encoding="utf-8") == "ABC"
    assert Path.cwd() == parent_cwd
    assert dict(os.environ) == parent_environment


def test_run_sequence_stops_at_first_unexpected_result_with_evidence(
    tmp_path: Path,
) -> None:
    command = (
        sys.executable,
        "-c",
        (
            "import pathlib, sys; "
            "pathlib.Path('stopped.txt').open('a').write(sys.argv[1]); "
            "print('stdout-' + sys.argv[1]); "
            "print('stderr-' + sys.argv[1], file=sys.stderr); "
            "sys.exit(int(sys.argv[2]))"
        ),
    )
    runner = ApmLifecycleRunner(command)

    with pytest.raises(AssertionError) as exc_info:
        runner.run_sequence(
            (("A", "0"), ("B", "7"), ("C", "0")),
            expected_returncodes=(0, 0, 0),
            scenario_id="stop-on-b",
            cwd=tmp_path,
            env=os.environ,
        )

    assert (tmp_path / "stopped.txt").read_text(encoding="utf-8") == "AB"
    assert str(exc_info.value) == (
        "scenario='stop-on-b'\n"
        f"cwd={str(tmp_path)!r}\n"
        "expected_returncode=0\n"
        "actual_returncode=7\n"
        f"command={(*command, 'B', '7')!r}\n"
        "stdout='stdout-B\\n'\n"
        "stderr='stderr-B\\n'"
    )


def test_run_sequence_rejects_misaligned_expectations(tmp_path: Path) -> None:
    runner = ApmLifecycleRunner()

    with pytest.raises(ValueError, match="equal length"):
        runner.run_sequence(
            (("--help",),),
            expected_returncodes=(),
            scenario_id="invalid",
            cwd=tmp_path,
            env=os.environ,
        )


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
