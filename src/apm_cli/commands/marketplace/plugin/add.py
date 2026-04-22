"""``apm marketplace plugin add`` command."""

from __future__ import annotations

import sys

import click

from ....core.command_logger import CommandLogger
from ....marketplace.errors import MarketplaceYmlError
from . import _ensure_yml_exists, _parse_tags, _resolve_ref, _verify_source, plugin


@plugin.command(help="Add a plugin to marketplace.yml")
@click.argument("source")
@click.option("--name", default=None, help="Package name (default: repo name)")
@click.option("--version", default=None, help="Semver range (e.g. '>=1.0.0')")
@click.option(
    "--ref",
    default=None,
    help="Pin to a git ref (SHA, tag, or HEAD). Mutable refs are auto-resolved to SHA.",
)
@click.option("--description", default=None, help="Human-readable description")
@click.option("--subdir", default=None, help="Subdirectory inside source repo")
@click.option("--tag-pattern", default=None, help="Tag pattern (e.g. 'v{version}')")
@click.option("--tags", default=None, help="Comma-separated tags")
@click.option("--include-prerelease", is_flag=True, help="Include prerelease versions")
@click.option("--no-verify", is_flag=True, help="Skip remote reachability check")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def add(
    source: str,
    name: str | None,
    version: str | None,
    ref: str | None,
    description: str | None,
    subdir: str | None,
    tag_pattern: str | None,
    tags: str | None,
    include_prerelease: bool,
    no_verify: bool,
    verbose: bool,
) -> None:
    """Add a plugin entry to marketplace.yml."""
    from ....marketplace.yml_editor import add_plugin_entry

    logger = CommandLogger("marketplace-plugin-add", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    if version and ref:
        raise click.UsageError(
            "--version and --ref are mutually exclusive. "
            "Use --version for semver ranges or --ref for git refs."
        )

    parsed_tags = _parse_tags(tags)

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
            description=description,
            subdir=subdir,
            tag_pattern=tag_pattern,
            tags=parsed_tags,
            include_prerelease=include_prerelease,
        )
    except MarketplaceYmlError as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(2)

    logger.success(f"Added plugin '{resolved_name}' from {source}", symbol="check")
