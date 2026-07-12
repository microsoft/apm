"""Process-wide stdout mode selected before any CLI notification."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

_MACHINE_FORMATS = frozenset({"json", "sarif"})


@dataclass(frozen=True)
class OutputMode:
    """Describe whether stdout is reserved for machine-readable output."""

    machine_readable: bool = False


def _option_has_value(
    args: tuple[str, ...],
    long_name: str,
    short_name: str,
    values: frozenset[str],
) -> bool:
    """Return whether a Click option carries one of the requested values."""
    for index, arg in enumerate(args):
        lower = arg.lower()
        if lower in {long_name, short_name}:
            if index + 1 < len(args) and args[index + 1].lower() in values:
                return True
            continue
        for prefix in (f"{long_name}=", f"{short_name}=", short_name):
            if lower.startswith(prefix) and lower[len(prefix) :] in values:
                return True
    return False


def _contains_command(args: tuple[str, ...], command: tuple[str, ...]) -> bool:
    """Return whether command tokens occur contiguously in the raw argv."""
    width = len(command)
    return any(args[index : index + width] == command for index in range(len(args) - width + 1))


def detect_output_mode(argv: Sequence[str]) -> OutputMode:
    """Detect machine output from the complete command-line intent."""
    args = tuple(argv)
    if "--json" in args:
        return OutputMode(machine_readable=True)
    if _option_has_value(args, "--format", "-f", _MACHINE_FORMATS):
        return OutputMode(machine_readable=True)
    if _contains_command(args, ("lock", "export")):
        return OutputMode(machine_readable=True)
    if _contains_command(args, ("policy", "status")) and _option_has_value(
        args,
        "--output",
        "-o",
        frozenset({"json"}),
    ):
        return OutputMode(machine_readable=True)
    return OutputMode()


def configure_output_mode(mode: OutputMode) -> None:
    """Apply process output routing before any console singleton is created."""
    from apm_cli.utils.console import set_console_stderr

    set_console_stderr(mode.machine_readable)
