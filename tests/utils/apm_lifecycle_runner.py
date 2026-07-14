from __future__ import annotations

import subprocess
import sys
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
    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._command = tuple(command or (sys.executable, "-m", "apm_cli.cli"))
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
        cwd: Path,
        env: Mapping[str, str],
    ) -> tuple[CommandResult, ...]:
        return tuple(self.run(command, cwd=cwd, env=env) for command in commands)
