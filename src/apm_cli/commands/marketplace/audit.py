"""``apm marketplace audit`` command."""

from __future__ import annotations

import sys
import traceback

import click

from ...core.command_logger import CommandLogger
from ...marketplace.audit import FetchStatus, run_audit
from ...marketplace.client import fetch_marketplace
from ...marketplace.registry import get_marketplace_by_name
from . import marketplace


@marketplace.command(help="Check that plugin dependencies resolve through the marketplace")
@click.argument("name", required=True)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero when any plugin has bypass dependencies or fetch errors",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def audit(name, strict, verbose):
    """Audit a registered marketplace's supply-chain pinning.

    For each plugin in ``NAME``'s manifest, fetch the plugin's own
    ``apm.yml`` (at its pinned ref) and warn when ``dependencies.apm``
    entries use direct repo paths, which bypass the marketplace's
    version pinning and make transitive deps track HEAD.
    """
    logger = CommandLogger("marketplace-audit", verbose=verbose)
    try:
        source = get_marketplace_by_name(name)
        logger.start(f"Auditing marketplace '{name}'...", symbol="running")

        manifest = fetch_marketplace(source, force_refresh=True)
        n = len(manifest.plugins)
        logger.progress(f"Checking {n} plugin{'' if n == 1 else 's'}...", symbol="info")

        reports = run_audit(manifest, source)

        ok_count = 0
        bypass_total = 0
        fetch_error_count = 0
        skipped_count = 0

        # Suppress the per-plugin section header when there is nothing to
        # report and the user did not opt into verbose: in the all-clean
        # default run the header would otherwise hang above an empty body.
        has_findings = any(rep.fetch_status != FetchStatus.OK or rep.issues for rep in reports)

        click.echo()
        if has_findings or verbose:
            click.echo("Audit Results:")
        for rep in reports:
            if rep.fetch_status == FetchStatus.OK:
                if not rep.issues:
                    ok_count += 1
                    if verbose:
                        logger.success(
                            f"  {rep.plugin_name}: deps are marketplace-resolved",
                            symbol="check",
                        )
                    continue
                bypass_total += len(rep.issues)
                if len(rep.issues) == 1:
                    verb_phrase = "1 dependency bypasses"
                else:
                    verb_phrase = f"{len(rep.issues)} dependencies bypass"
                logger.warning(
                    f"  {rep.plugin_name}: {verb_phrase} the marketplace",
                    symbol="warning",
                )
                for issue in rep.issues:
                    click.echo(f"      - '{issue.dep}'")
                    click.echo(f"        hint: {issue.suggestion}")
            elif rep.fetch_status in (
                FetchStatus.NO_MANIFEST,
                FetchStatus.UNSUPPORTED_SOURCE,
            ):
                skipped_count += 1
                if verbose:
                    logger.verbose_detail(
                        f"  {rep.plugin_name}: skipped ({rep.fetch_status.value}"
                        f"{' - ' + rep.detail if rep.detail else ''})"
                    )
            else:
                fetch_error_count += 1
                logger.warning(
                    f"  {rep.plugin_name}: could not verify "
                    f"({rep.fetch_status.value}: {rep.detail})",
                    symbol="warning",
                )

        click.echo()
        warn_noun = "warning" if bypass_total == 1 else "warnings"
        err_noun = "error" if fetch_error_count == 1 else "errors"
        logger.success(
            f"Summary: {ok_count} clean, {bypass_total} bypass {warn_noun}, "
            f"{skipped_count} skipped, "
            f"{fetch_error_count} unverifiable {err_noun}",
            symbol="check",
        )
        if bypass_total:
            click.echo()
            click.echo(
                "Marketplace refs (name@marketplace) pin transitive deps "
                "through the catalogue so consumers get the same versions "
                "you tested.  See: "
                "https://microsoft.github.io/apm/reference/cli/marketplace/#apm-marketplace-audit-name"
            )

        if strict and (bypass_total or fetch_error_count):
            sys.exit(1)

    except Exception as e:
        logger.error(f"Failed to audit marketplace: {e}")
        logger.info("Run with --verbose for details.")
        logger.verbose_detail(traceback.format_exc())
        sys.exit(1)
