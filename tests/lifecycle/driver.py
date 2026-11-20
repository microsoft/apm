"""Run one APM command line against a lifecycle world (#4093).

``APM_LIFECYCLE_DRIVER`` selects the entry point: ``engine`` (default, the
PR-time ``python -m apm_cli.cli``) or ``binary`` (the packaged ``apm`` from
``APM_BINARY_PATH`` or ``PATH``, for the nightly).

Every result goes through ``OUTPUT_HOOKS``; ``tests/lifecycle/conftest.py``
installs the global output oracle there.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult

DRIVER_ENV = "APM_LIFECYCLE_DRIVER"
DRIVERS = ("engine", "binary")
TIMEOUT_SECONDS = 120.0
# The flag that moves a command to user scope; other commands have no user scope.
_USER_SCOPE_FLAGS = {
    "install": "--global",
    "update": "--global",
    "uninstall": "--global",
    "deps": "--global",
    "lock": "--global",
    "compile": "--global",
    "outdated": "--global",
    "approve": "--user",
    "deny": "--user",
}

OutputHook = Callable[[tuple[str, ...], CommandResult, str], None]
OUTPUT_HOOKS: list[OutputHook] = []


class DriverWorld(Protocol):
    project: Path
    env: dict[str, str]


def command_cwd(world: DriverWorld, scope: str) -> Path:
    """Project commands run in the project; user-scope ones outside it (no apm.yml).

    ``apm approve --user`` reads the cwd's apm.yml first (commands/approve.py).
    """
    user_cwd = getattr(world, "user_cwd", None)
    return user_cwd if scope == "user" and user_cwd is not None else world.project


def selected_driver() -> str:
    driver = os.environ.get(DRIVER_ENV, "engine")
    if driver not in DRIVERS:
        raise ValueError(f"{DRIVER_ENV} must be one of {DRIVERS}, got {driver!r}")
    return driver


def driver_command(driver: str | None = None) -> tuple[str, ...]:
    """The entry point of *driver* (default: the selected one)."""
    driver = driver or selected_driver()
    if driver == "engine":
        return (sys.executable, "-m", "apm_cli.cli")
    binary = os.environ.get("APM_BINARY_PATH") or shutil.which("apm")
    if not binary:
        raise RuntimeError("binary driver needs APM_BINARY_PATH or apm on PATH")
    return (binary,)


def command_args(argv: str | Sequence[str], *, scope: str = "project") -> tuple[str, ...]:
    """Normalize ``"apm install x"`` or a sequence to CLI args, adding the scope flag."""
    args = shlex.split(argv) if isinstance(argv, str) else list(argv)
    if args and args[0] == "apm":
        args = args[1:]
    if not args:
        raise ValueError("empty APM command line")
    if scope == "user":
        flag = _USER_SCOPE_FLAGS.get(args[0])
        if flag is None:
            raise ValueError(f"apm {args[0]} has no user-scope form")
        if flag not in args and "-g" not in args:
            args.insert(1, flag)
    elif scope != "project":
        raise ValueError(f"unknown scope {scope!r}")
    return tuple(args)


def run(
    world: DriverWorld,
    argv: str | Sequence[str],
    *,
    scope: str = "project",
    entry_point: Sequence[str] | None = None,
) -> CommandResult:
    """Run *argv* in *world* and pass the result to every output hook."""
    args = command_args(argv, scope=scope)
    runner = ApmLifecycleRunner(entry_point or driver_command(), timeout_seconds=TIMEOUT_SECONDS)
    result = runner.run(
        args, cwd=command_cwd(world, scope), env=world.env, scenario_id=f"lifecycle-{args[0]}"
    )
    for hook in tuple(OUTPUT_HOOKS):
        hook(args, result, scope)
    return result
