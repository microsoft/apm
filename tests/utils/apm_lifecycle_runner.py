from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    cwd: Path


class _LifecycleTimeoutExpired(subprocess.TimeoutExpired):
    """Timeout with stable scenario evidence for pytest diagnostics."""

    def __init__(
        self,
        command: tuple[str, ...],
        timeout: float,
        *,
        scenario_id: str,
        cwd: Path,
        budget_seconds: float,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(command, timeout, output=stdout, stderr=stderr)
        self.scenario_id = scenario_id
        self.cwd = cwd
        self.budget_seconds = budget_seconds

    def __str__(self) -> str:
        return (
            f"scenario={self.scenario_id!r}\n"
            f"cwd={str(self.cwd)!r}\n"
            f"command={self.cmd!r}\n"
            f"budget_seconds={self.budget_seconds}\n"
            f"stdout={self.output!r}\n"
            f"stderr={self.stderr!r}"
        )


class ApmLifecycleRunner:
    """Run an APM entry point with explicit process inputs and captured evidence.

    The default resolves the development environment's ``apm`` console script.
    Pass an explicit command tuple to exercise a source module or packaged
    standalone binary instead.
    """

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        timeout_seconds: float = 120.0,
        scenario_timeout_seconds: float = 300.0,
    ) -> None:
        self._command = tuple(command or ("apm",))
        self._timeout_seconds = timeout_seconds
        self._scenario_timeout_seconds = scenario_timeout_seconds

    def run(
        self,
        args: Sequence[str],
        *,
        scenario_id: str = "single-command",
        cwd: Path,
        env: Mapping[str, str],
    ) -> CommandResult:
        return self._run_with_timeout(
            args,
            cwd=cwd,
            env=env,
            timeout_seconds=self._timeout_seconds,
            scenario_id=scenario_id,
            budget_seconds=self._timeout_seconds,
        )

    def _run_with_timeout(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        scenario_id: str,
        budget_seconds: float,
    ) -> CommandResult:
        command = (*self._command, *args)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name != "nt",
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_tree(process)
            stdout, stderr = process.communicate()
            raise _LifecycleTimeoutExpired(
                command,
                timeout_seconds,
                scenario_id=scenario_id,
                cwd=cwd,
                budget_seconds=budget_seconds,
                stdout=stdout,
                stderr=stderr,
            ) from exc
        return CommandResult(
            command=command,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
        )

    def run_sequence(
        self,
        commands: Sequence[Sequence[str]],
        *,
        expected_returncodes: Sequence[int],
        scenario_id: str,
        cwd: Path,
        env: Mapping[str, str],
    ) -> tuple[CommandResult, ...]:
        if len(commands) != len(expected_returncodes):
            raise ValueError("commands and expected_returncodes must have equal length")

        deadline = time.monotonic() + self._scenario_timeout_seconds
        results: list[CommandResult] = []
        for command, expected_returncode in zip(
            commands,
            expected_returncodes,
            strict=True,
        ):
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise _LifecycleTimeoutExpired(
                    (*self._command, *command),
                    0,
                    scenario_id=scenario_id,
                    cwd=cwd,
                    budget_seconds=self._scenario_timeout_seconds,
                )
            result = self._run_with_timeout(
                command,
                cwd=cwd,
                env=env,
                timeout_seconds=min(self._timeout_seconds, remaining_seconds),
                scenario_id=scenario_id,
                budget_seconds=self._scenario_timeout_seconds,
            )
            results.append(result)
            if result.returncode != expected_returncode:
                raise AssertionError(
                    _unexpected_result_evidence(
                        result,
                        scenario_id=scenario_id,
                        expected_returncode=expected_returncode,
                    )
                )
        return tuple(results)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate one isolated process group, including descendants."""
    if os.name == "nt":
        subprocess.run(
            ("taskkill", "/PID", str(process.pid), "/T", "/F"),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if process.poll() is None:
            process.kill()
        return

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        if process.poll() is None:
            process.kill()


def _unexpected_result_evidence(
    result: CommandResult,
    *,
    scenario_id: str,
    expected_returncode: int,
) -> str:
    return (
        f"scenario={scenario_id!r}\n"
        f"cwd={str(result.cwd)!r}\n"
        f"expected_returncode={expected_returncode}\n"
        f"actual_returncode={result.returncode}\n"
        f"command={result.command!r}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
