from __future__ import annotations

import inspect
import os
import subprocess
import sys
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult


def test_runner_executes_injected_apm_binary(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("--help",),
        cwd=tmp_path,
        env=os.environ,
    )

    assert result.command == (str(apm_binary_path), "--help")
    assert result.returncode == 0
    assert "Usage: apm [OPTIONS] COMMAND [ARGS]..." in result.stdout
    assert "Agent Package Manager (APM)" in result.stdout
    assert result.cwd == tmp_path


def test_runner_requires_explicit_command() -> None:
    command = inspect.signature(ApmLifecycleRunner).parameters["command"]

    assert command.default is inspect.Parameter.empty


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
    command = (
        sys.executable,
        "-c",
        (
            "import sys, time; "
            "print('before-timeout', flush=True); "
            "print('timeout-stderr', file=sys.stderr, flush=True); "
            "time.sleep(10)"
        ),
    )
    runner = ApmLifecycleRunner(command, timeout_seconds=1.0)

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        runner.run(
            (),
            scenario_id="single-timeout",
            cwd=tmp_path,
            env=os.environ,
        )

    assert exc_info.value.cmd == command
    assert exc_info.value.timeout == 1.0
    assert str(exc_info.value) == (
        "scenario='single-timeout'\n"
        f"cwd={str(tmp_path)!r}\n"
        f"command={command!r}\n"
        "budget_seconds=1.0\n"
        "stdout='before-timeout\\n'\n"
        "stderr='timeout-stderr\\n'"
    )


def test_run_sequence_enforces_one_scenario_deadline(tmp_path: Path) -> None:
    runner = ApmLifecycleRunner(
        (sys.executable, "-c", "import time; time.sleep(2.0)"),
        timeout_seconds=1.0,
        scenario_timeout_seconds=0.5,
    )

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        runner.run_sequence(
            ((), ()),
            expected_returncodes=(0, 0),
            scenario_id="bounded-sequence",
            cwd=tmp_path,
            env=os.environ,
        )

    assert time.monotonic() - started < 2.0
    assert 0 < exc_info.value.timeout < 1.0
    message = str(exc_info.value)
    assert "scenario='bounded-sequence'" in message
    assert f"cwd={str(tmp_path)!r}" in message
    assert "command=" in message
    assert "budget_seconds=0.5" in message
    assert "stdout=" in message
    assert "stderr=" in message


def test_expired_scenario_deadline_reports_context_before_spawn(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        scenario_timeout_seconds=0,
    )

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        runner.run_sequence(
            (("--help",),),
            expected_returncodes=(0,),
            scenario_id="already-expired",
            cwd=tmp_path,
            env=os.environ,
        )

    assert str(exc_info.value) == (
        "scenario='already-expired'\n"
        f"cwd={str(tmp_path)!r}\n"
        f"command={(str(apm_binary_path), '--help')!r}\n"
        "budget_seconds=0\n"
        "stdout=''\n"
        "stderr=''"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
def test_timeout_terminates_descendant_process_tree(tmp_path: Path) -> None:
    script = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path('descendant.pid').write_text(str(child.pid)); "
        "time.sleep(60)"
    )
    runner = ApmLifecycleRunner(
        (sys.executable, "-c", script),
        timeout_seconds=0.3,
    )

    with pytest.raises(subprocess.TimeoutExpired):
        runner.run((), cwd=tmp_path, env=os.environ)

    descendant_pid = int((tmp_path / "descendant.pid").read_text())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"descendant process {descendant_pid} survived timeout")


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
    runner = ApmLifecycleRunner(("unused",))

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
