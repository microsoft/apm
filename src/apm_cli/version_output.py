"""Canonical version renderer, independent of Git-dependent command imports."""

from pathlib import Path

import click

from .version import get_build_sha, get_version

_BANNER = "Agent Package Manager (APM) CLI"


def print_version(ctx: click.Context, param: click.Parameter | None, value: bool) -> None:
    """Print version and exit, preserving optional verbose-version details."""
    if not value or ctx.resilient_parsing:
        return

    version_str = get_version()
    sha = get_build_sha()
    if sha:
        version_str += f" ({sha})"

    # click.style + click.echo, not a rich Console: the banner is the whole
    # output of ``apm --version``, which should load nothing but click. Click
    # strips the styling when stdout is not a terminal.
    click.echo(f"{click.style(_BANNER, fg='cyan', bold=True)} version {version_str}")

    try:
        from .core.experimental import is_enabled

        if is_enabled("verbose_version"):
            import platform
            import sys

            from .utils.console import _rich_echo

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
