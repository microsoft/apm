"""APM list command."""

import builtins
import sys

import click

from ..core.command_logger import CommandLogger
from ..utils.console import (
    STATUS_SYMBOLS,
    _rich_echo,  # noqa: F401
    _rich_panel,
)
from ._helpers import HIGHLIGHT, RESET, _get_console, _list_available_scripts

# Restore builtin since the Click command function is named ``list``
list = builtins.list


@click.command(help="List available scripts in the current project")
@click.pass_context
def list(ctx):  # noqa: F811
    """List all available scripts from apm.yml."""
    logger = CommandLogger("list")
    try:
        scripts = _list_available_scripts()

        if not scripts:
            logger.warning("No scripts found.")

            # Show helpful example in a panel
            example_content = """scripts:
  start: "codex run main.prompt.md"
  fast: "llm prompt main.prompt.md -m github/gpt-4o-mini" """

            try:
                _rich_panel(
                    example_content,
                    title=f"{STATUS_SYMBOLS['info']} Add scripts to your apm.yml file",
                    style="blue",
                )
            except (ImportError, NameError):
                logger.progress("Add scripts to your apm.yml file:")
                click.echo("scripts:")
                click.echo('  start: "codex run main.prompt.md"')
                click.echo('  fast: "llm prompt main.prompt.md -m github/gpt-4o-mini"')
            return

        # Show default script if 'start' exists
        default_script = "start" if "start" in scripts else None

        console = _get_console()
        if console:
            try:
                from rich.table import Table

                # Create a nice table for scripts
                table = Table(
                    title=" Available Scripts",
                    show_header=True,
                    header_style="bold cyan",
                )
                table.add_column("", style="cyan", width=3)
                table.add_column("Script", style="bold white", min_width=12)
                table.add_column("Command", style="white")

                for name, command in scripts.items():
                    icon = STATUS_SYMBOLS["default"] if name == default_script else "  "
                    table.add_row(icon, name, command)

                console.print(table)

                if default_script:
                    console.print(
                        f"\n[muted]{STATUS_SYMBOLS['info']} {STATUS_SYMBOLS['default']} = default script (runs when no script name specified)[/muted]"
                    )

            except Exception:
                # Fallback to simple output
                logger.progress("Available scripts:")
                for name, command in scripts.items():
                    icon = STATUS_SYMBOLS["default"] if name == default_script else "  "
                    click.echo(f"  {icon} {HIGHLIGHT}{name}{RESET}: {command}")
                if default_script:
                    click.echo(
                        f"\n{STATUS_SYMBOLS['info']} {STATUS_SYMBOLS['default']} = default script"
                    )
        else:
            # Fallback to simple output
            logger.progress("Available scripts:")
            for name, command in scripts.items():
                icon = STATUS_SYMBOLS["default"] if name == default_script else "  "
                click.echo(f"  {icon} {HIGHLIGHT}{name}{RESET}: {command}")
            if default_script:
                click.echo(
                    f"\n{STATUS_SYMBOLS['info']} {STATUS_SYMBOLS['default']} = default script"
                )

    except Exception as e:
        logger.error(f"Error listing scripts: {e}")
        sys.exit(1)
