"""``apm marketplace package add`` command."""

from __future__ import annotations

import sys

import click

from ....core.command_logger import CommandLogger
from ....marketplace.errors import MarketplaceYmlError
from . import (
    _ensure_yml_exists,
    _parse_tags,
    _resolve_ref,
    _verify_source,
    package,
)


@package.command(help="Add a package to marketplace authoring config")
@click.argument("source", required=False)
@click.option("--name", default=None, help="Package name (default: repo name or plugin name)")
@click.option("--version", default=None, help="Semver range (e.g. '>=1.0.0')")
@click.option(
    "--ref",
    default=None,
    help="Pin to a git ref (SHA, tag, or HEAD). Mutable refs are auto-resolved to SHA.",
)
@click.option("-s", "--subdir", default=None, help="Subdirectory inside source repo")
@click.option("--tag-pattern", default=None, help="Tag pattern (e.g. 'v{version}')")
@click.option("--tags", default=None, help="Comma-separated tags")
@click.option("--include-prerelease", is_flag=True, help="Include prerelease versions")
@click.option(
    "--upstream",
    default=None,
    help="Expose a plugin from a registered upstream (mutually exclusive with SOURCE)",
)
@click.option(
    "--plugin",
    default=None,
    help="Plugin name in the upstream marketplace (required when --upstream is set)",
)
@click.option("--allow-head", is_flag=True, help="Allow upstream plugin to track a mutable ref")
@click.option("--no-verify", is_flag=True, help="Skip remote reachability check")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def add(
    source,
    name,
    version,
    ref,
    subdir,
    tag_pattern,
    tags,
    include_prerelease,
    upstream,
    plugin,
    allow_head,
    no_verify,
    verbose,
):
    """Add a package entry to marketplace authoring config."""
    from ....marketplace.yml_editor import add_plugin_entry, add_upstream_package_entry

    logger = CommandLogger("marketplace-package-add", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    # SOURCE xor --upstream.
    if upstream and source:
        raise click.UsageError(
            "SOURCE and --upstream are mutually exclusive. "
            "Use SOURCE for direct git packages, or --upstream <alias> for "
            "plugins from a registered upstream marketplace."
        )
    if not upstream and not source:
        raise click.UsageError(
            "Provide either a SOURCE (e.g. 'owner/repo') or '--upstream <alias>'."
        )
    if source and (plugin or allow_head):
        raise click.UsageError(
            "--plugin and --allow-head only apply to upstream packages (use with --upstream)."
        )
    if upstream and plugin is None:
        raise click.UsageError(
            "--plugin is required when --upstream is set. "
            "Specify the plugin name as it appears in the upstream marketplace."
        )
    if subdir and upstream:
        raise click.UsageError("--subdir only applies to direct packages (not --upstream).")

    # --version and --ref are mutually exclusive on either path.
    if version and ref:
        raise click.UsageError(
            "--version and --ref are mutually exclusive. "
            "Use --version for semver ranges or --ref for git refs."
        )

    parsed_tags = _parse_tags(tags)

    if upstream:
        try:
            resolved_name = add_upstream_package_entry(
                yml,
                upstream=upstream,
                plugin=plugin,
                name=name,
                version=version,
                ref=ref,
                tag_pattern=tag_pattern,
                tags=parsed_tags,
                include_prerelease=include_prerelease,
                allow_head=allow_head,
            )
        except MarketplaceYmlError as exc:
            logger.error(str(exc), symbol="error")
            sys.exit(2)
        logger.success(
            f"Added package '{resolved_name}' from upstream '{upstream}'",
            symbol="check",
        )
        logger.info("Next: 'apm pack' to build marketplace.json", symbol="info")
        return

    # Direct package path (existing behaviour, unchanged).
    if not no_verify:
        _verify_source(logger, source)

    ref = _resolve_ref(logger, source, ref, version, no_verify)

    try:
        resolved_name = add_plugin_entry(
            yml,
            source=source,
            name=name,
            version=version,
            ref=ref,
            subdir=subdir,
            tag_pattern=tag_pattern,
            tags=parsed_tags,
            include_prerelease=include_prerelease,
        )
    except MarketplaceYmlError as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(2)

    logger.success(
        f"Added package '{resolved_name}' from {source}",
        symbol="check",
    )
