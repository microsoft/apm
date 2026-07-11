"""Process-wide stdout mode selected before any CLI notification."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class OutputMode:
    """Describe whether stdout is reserved for machine-readable output."""

    machine_readable: bool = False


def detect_output_mode(argv: Sequence[str]) -> OutputMode:
    """Detect machine output from the complete command-line intent."""
    args = tuple(argv)
    if "--json" in args:
        return OutputMode(machine_readable=True)
    for index, arg in enumerate(args[:-1]):
        if arg in {"--format", "-f"} and args[index + 1].lower() in {
            "json",
            "sarif",
        }:
            return OutputMode(machine_readable=True)
    command_tokens = tuple(arg for arg in args if not arg.startswith("-"))
    if len(command_tokens) >= 2 and command_tokens[:2] == ("lock", "export"):
        return OutputMode(machine_readable=True)
    if len(args) >= 2 and args[:2] == ("policy", "status"):
        for index, arg in enumerate(args[:-1]):
            if arg in {"--output", "-o"} and args[index + 1].lower() == "json":
                return OutputMode(machine_readable=True)
    return OutputMode()


def configure_output_mode(mode: OutputMode) -> None:
    """Apply process output routing before any console singleton is created."""
    from apm_cli.utils.console import set_console_stderr

    set_console_stderr(mode.machine_readable)
