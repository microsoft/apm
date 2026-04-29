"""``apm marketplace build`` command."""

from __future__ import annotations

import traceback

import click

from ...core.command_logger import CommandLogger
from ...marketplace.builder import BuildOptions, MarketplaceBuilder
from ...marketplace.errors import BuildError, MarketplaceYmlError
from . import (
    marketplace,
    _load_config_or_exit,
    _render_build_error,
    _render_build_table,
    _require_authoring_flag,
)


@marketplace.command(help="Build marketplace.json from apm.yml or marketplace.yml")
@click.option("--dry-run", is_flag=True, help="Preview without writing marketplace.json")
@click.option("--offline", is_flag=True, help="Use cached refs only (no network)")
@click.option(
    "--include-prerelease", is_flag=True, help="Include prerelease versions"
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def build(dry_run, offline, include_prerelease, verbose):
    """Resolve packages and compile marketplace.json."""
    _require_authoring_flag()
    logger = CommandLogger("marketplace-build", verbose=verbose)
    project_root, config = _load_config_or_exit(logger)
    config_path = config.source_path or project_root / "apm.yml"

    try:
        opts = BuildOptions(
            dry_run=dry_run,
            offline=offline,
            include_prerelease=include_prerelease,
        )
        builder = MarketplaceBuilder(config_path, options=opts)
        report = builder.build()
    except MarketplaceYmlError as exc:
        logger.error(f"marketplace.yml schema error: {exc}", symbol="error")
        sys.exit(2)
    except BuildError as exc:
        _render_build_error(logger, exc)
        logger.verbose_detail(traceback.format_exc())
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 -- top-level command catch-all
        logger.error(f"Build failed: {e}", symbol="error")
        logger.verbose_detail(traceback.format_exc())
        sys.exit(1)

    # Render results table
    _render_build_table(logger, report)

    # Surface duplicate-name warnings from the builder
    for warn_msg in report.warnings:
        logger.warning(warn_msg, symbol="warning")

    if dry_run:
        logger.progress(
            "Dry run -- marketplace.json not written", symbol="info"
        )
    else:
        logger.success(
            f"Built marketplace.json ({len(report.resolved)} packages)",
            symbol="check",
        )
