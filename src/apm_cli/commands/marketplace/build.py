"""``apm marketplace build`` command."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import click

from ...core.command_logger import CommandLogger
from ...marketplace.builder import BuildOptions
from ...marketplace.errors import BuildError, MarketplaceYmlError
from . import marketplace, _load_yml_or_exit, _render_build_error, _render_build_table


@marketplace.command(help="Build marketplace.json from marketplace.yml")
@click.option("--dry-run", is_flag=True, help="Preview without writing marketplace.json")
@click.option("--offline", is_flag=True, help="Use cached refs only (no network)")
@click.option(
    "--include-prerelease",
    is_flag=True,
    help="Include prerelease versions",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def build(
    dry_run: bool,
    offline: bool,
    include_prerelease: bool,
    verbose: bool,
) -> None:
    """Resolve packages and compile marketplace.json."""
    from . import MarketplaceBuilder

    logger = CommandLogger("marketplace-build", verbose=verbose)
    yml_path = Path.cwd() / "marketplace.yml"

    _load_yml_or_exit(logger)

    try:
        opts = BuildOptions(
            dry_run=dry_run,
            offline=offline,
            include_prerelease=include_prerelease,
        )
        builder = MarketplaceBuilder(yml_path, options=opts)
        report = builder.build()
    except MarketplaceYmlError as exc:
        logger.error(f"marketplace.yml schema error: {exc}", symbol="error")
        sys.exit(2)
    except BuildError as exc:
        _render_build_error(logger, exc)
        if verbose:
            click.echo(traceback.format_exc(), err=True)
        sys.exit(1)
    except Exception as exc:
        logger.error(f"Build failed: {exc}", symbol="error")
        if verbose:
            click.echo(traceback.format_exc(), err=True)
        sys.exit(1)

    _render_build_table(logger, report)

    if dry_run:
        logger.progress("Dry run -- marketplace.json not written", symbol="info")
    else:
        logger.success(
            f"Built marketplace.json ({len(report.resolved)} packages)",
            symbol="check",
        )
