"""Hidden ``apm discover`` alias for ``apm init --discover``."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from ..core.target_detection import TargetParamType

DISCOVER_HELP = (
    "Inventory existing agent-harness files (Claude, Copilot, Cursor, Codex, ...) "
    "and propose an apm.yml; read-only unless --apply"
)


def discover_options(command: click.Command | Callable[..., Any]) -> Any:
    """Attach the shared discovery options to *command*."""
    for option in reversed(
        (
            click.option(
                "--apply",
                "--write",
                "write",
                is_flag=True,
                help="Apply the plan: copy hand-authored harness files into .apm/ and update apm.yml "
                "(--write is an alias)",
            ),
            click.option(
                "--format",
                "output_format",
                type=click.Choice(["text", "json", "yaml"]),
                default="text",
                show_default=True,
                help="Discovery report format",
            ),
            click.option(
                "--global",
                "-g",
                "global_",
                is_flag=True,
                help="Scan the user scope (~/) instead of the project",
            ),
            click.option(
                "--include-hook-scripts",
                is_flag=True,
                help="With --apply: copy in-project hook scripts into .apm/hooks/scripts/",
            ),
        )
    ):
        command = option(command)
    return command


def run_discover(
    *,
    write: bool,
    output_format: str,
    global_: bool,
    include_hook_scripts: bool,
    target_flag,
    yes: bool,
    verbose: bool,
) -> None:
    """Invoke the adoption engine and exit with its status."""
    from ..adopt import run_discover_command

    code = run_discover_command(
        project_root=Path.cwd(),
        write=write,
        fmt=output_format,
        user_scope=global_,
        target_flag=target_flag,
        yes=yes,
        verbose=verbose,
        include_hook_scripts=include_hook_scripts,
    )
    if code:
        sys.exit(code)


@click.command(help=DISCOVER_HELP, hidden=True)
@discover_options
@click.option(
    "--target",
    "target_flag",
    type=TargetParamType(),
    default=None,
    help="Preferred target order for conflicts",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def discover(write, output_format, global_, include_hook_scripts, target_flag, yes, verbose):
    """Alias of ``apm init --discover``."""
    run_discover(
        write=write,
        output_format=output_format,
        global_=global_,
        include_hook_scripts=include_hook_scripts,
        target_flag=target_flag,
        yes=yes,
        verbose=verbose,
    )
