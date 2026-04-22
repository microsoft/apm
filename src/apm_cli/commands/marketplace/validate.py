"""``apm marketplace validate`` command."""

from __future__ import annotations

import sys
import traceback

import click

from ...core.command_logger import CommandLogger
from . import marketplace


@marketplace.command(help="Validate a marketplace manifest")
@click.argument("name", required=True)
@click.option(
    "--check-refs",
    is_flag=True,
    help="Verify version refs are reachable (network)",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def validate(name: str, check_refs: bool, verbose: bool) -> None:
    """Validate the manifest of a registered marketplace."""
    logger = CommandLogger("marketplace-validate", verbose=verbose)
    try:
        from ...marketplace.client import fetch_marketplace
        from ...marketplace.registry import get_marketplace_by_name
        from ...marketplace.validator import validate_marketplace

        source = get_marketplace_by_name(name)
        logger.start(f"Validating marketplace '{name}'...", symbol="gear")

        manifest = fetch_marketplace(source, force_refresh=True)

        logger.progress(f"Found {len(manifest.plugins)} plugins", symbol="info")

        if verbose:
            for plugin in manifest.plugins:
                source_type = "dict" if isinstance(plugin.source, dict) else "string"
                logger.verbose_detail(f"    {plugin.name}: source type: {source_type}")

        results = validate_marketplace(manifest)

        if check_refs:
            logger.warning(
                "Ref checking not yet implemented -- skipping ref "
                "reachability checks",
                symbol="warning",
            )

        passed = 0
        warning_count = 0
        error_count = 0
        click.echo()
        click.echo("Validation Results:")
        for result in results:
            if result.passed and not result.warnings:
                logger.success(f"  {result.check_name}: all plugins valid", symbol="check")
                passed += 1
            elif result.warnings and not result.errors:
                for warning in result.warnings:
                    logger.warning(f"  {result.check_name}: {warning}", symbol="warning")
                warning_count += len(result.warnings)
            else:
                for error in result.errors:
                    logger.error(f"  {result.check_name}: {error}", symbol="error")
                for warning in result.warnings:
                    logger.warning(f"  {result.check_name}: {warning}", symbol="warning")
                error_count += len(result.errors)
                warning_count += len(result.warnings)

        click.echo()
        click.echo(
            f"Summary: {passed} passed, {warning_count} warnings, "
            f"{error_count} errors"
        )

        if error_count > 0:
            sys.exit(1)

    except Exception as exc:
        logger.error(f"Failed to validate marketplace: {exc}")
        if verbose:
            click.echo(traceback.format_exc(), err=True)
        sys.exit(1)
