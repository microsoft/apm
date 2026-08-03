"""``apm marketplace validate`` command."""

from __future__ import annotations

import sys
import traceback

import click

from ...core.command_logger import CommandLogger
from . import marketplace


@marketplace.command(help="Validate marketplace structure and plugin schema")
@click.argument("name", required=True)
@click.option(
    "--check-refs", is_flag=True, hidden=True, help="Verify version refs are reachable (network)"
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def validate(name, check_refs, verbose):
    """Report malformed manifest structure and plugin schema errors."""
    logger = CommandLogger("marketplace-validate", verbose=verbose)
    try:
        from ...marketplace.client import fetch_marketplace
        from ...marketplace.registry import get_marketplace_by_name
        from ...marketplace.validator import validate_marketplace

        source = get_marketplace_by_name(name)
        logger.start(f"Validating marketplace '{name}'...", symbol="gear")

        manifest = fetch_marketplace(source, force_refresh=True)
        results = validate_marketplace(manifest)
        has_structure_errors = any(
            result.check_name == "Structure" and result.errors for result in results
        )

        if not has_structure_errors:
            logger.progress(
                f"Found {len(manifest.plugins)} plugins",
                symbol="info",
            )

        # Verbose: per-plugin details
        if verbose and not has_structure_errors:
            for p in manifest.plugins:
                source_type = "dict" if isinstance(p.source, dict) else "string"
                logger.verbose_detail(f"    {p.name}: source type: {source_type}")

        # Check-refs placeholder
        if check_refs:
            logger.warning(
                "Ref checking not yet implemented -- skipping ref reachability checks",
                symbol="warning",
            )

        # Render results
        passed = 0
        warning_count = 0
        error_count = 0
        logger.blank_line()
        logger.progress("Validation Results:", symbol="info")
        for r in results:
            if r.passed and not r.warnings:
                if has_structure_errors:
                    continue
                logger.success(f"  {r.check_name}: passed", symbol="check")
                passed += 1
            elif r.warnings and not r.errors:
                for w in r.warnings:
                    logger.warning(f"  {r.check_name}: {w}", symbol="warning")
                warning_count += len(r.warnings)
            else:
                for e in r.errors:
                    logger.error(f"  {r.check_name}: {e}", symbol="error")
                for w in r.warnings:
                    logger.warning(f"  {r.check_name}: {w}", symbol="warning")
                error_count += len(r.errors)
                warning_count += len(r.warnings)

        logger.blank_line()
        logger.progress(
            f"Summary: {passed} passed, {warning_count} warnings, {error_count} errors",
            symbol="info",
        )

        if error_count > 0:
            sys.exit(1)

    except Exception as e:
        logger.error(f"Failed to validate marketplace: {e}", symbol="error")
        logger.verbose_detail(traceback.format_exc())
        sys.exit(1)
