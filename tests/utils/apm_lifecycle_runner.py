from __future__ import annotations

import subprocess
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
    ) -> None:
        self._command = tuple(command or ("apm",))
        self._timeout_seconds = timeout_seconds

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> CommandResult:
        command = (*self._command, *args)
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=self._timeout_seconds,
            check=False,
        )
        return CommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
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

        results: list[CommandResult] = []
        for command, expected_returncode in zip(
            commands,
            expected_returncodes,
            strict=True,
        ):
            result = self.run(command, cwd=cwd, env=env)
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
