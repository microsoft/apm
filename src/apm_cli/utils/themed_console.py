"""Themed stdout Rich console shared by command output and the install summary.

Moved from ``commands/_helpers.py`` (#3879) so the install layer can print
through it without importing the commands layer.
"""

import click

# Lazy loading for Rich components to improve startup performance
_console = None


def _get_console():
    """Get Rich console instance with lazy loading."""
    global _console
    if _console is None:
        from rich.theme import Theme

        from apm_cli.utils.console import new_console

        custom_theme = Theme(
            {
                "info": "cyan",
                "warning": "yellow",
                "error": "bold red",
                "success": "bold green",
                "highlight": "bold magenta",
                "muted": "dim white",
                "accent": "bold blue",
                "title": "bold cyan",
            }
        )

        _console = new_console(theme=custom_theme)
    return _console


def _rich_blank_line():
    """Print a blank line with Rich if available, otherwise use click."""
    console = _get_console()
    if console:
        console.print()
    else:
        click.echo()
