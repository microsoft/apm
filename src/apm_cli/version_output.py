"""Canonical version renderer, independent of Git-dependent command imports."""

from pathlib import Path

import click
from colorama import Fore, Style

from .utils.console import _get_console, _rich_echo
from .version import get_build_sha, get_version


def print_version(ctx: click.Context, param: click.Parameter | None, value: bool) -> None:
    """Print version and exit, preserving optional verbose-version details."""
    if not value or ctx.resilient_parsing:
        return

    version_str = get_version()
    sha = get_build_sha()
    if sha:
        version_str += f" ({sha})"

    console = _get_console()
    if console:
        try:
            console.print(
                f"[bold cyan]Agent Package Manager (APM) CLI[/bold cyan] version {version_str}"
            )
        except Exception:
            click.echo(
                f"{Fore.CYAN}{Style.BRIGHT}Agent Package Manager (APM) CLI"
                f"{Style.RESET_ALL} version {version_str}"
            )
    else:
        click.echo(
            f"{Fore.CYAN}{Style.BRIGHT}Agent Package Manager (APM) CLI"
            f"{Style.RESET_ALL} version {version_str}"
        )

    try:
        from .core.experimental import is_enabled

        if is_enabled("verbose_version"):
            import platform
            import sys

            python_ver = platform.python_version()
            plat = f"{sys.platform}-{platform.machine()}"
            install_path = str(Path(__file__).resolve().parent)

            _rich_echo(f"  {'Python:':<14}{python_ver}", color="dim")
            _rich_echo(f"  {'Platform:':<14}{plat}", color="dim")
            _rich_echo(f"  {'Install path:':<14}{install_path}", color="dim")
    except Exception:
        # Experimental config failures must not prevent baseline version output.
        pass

    ctx.exit()
