"""``apm marketplace plugin set`` command."""

from __future__ import annotations

import sys

import click

from ....core.command_logger import CommandLogger
from ....marketplace.errors import MarketplaceYmlError
from . import _SHA_RE, _ensure_yml_exists, _parse_tags, _resolve_ref, plugin


@plugin.command("set", help="Update a plugin entry in marketplace.yml")
@click.argument("name")
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
@click.option(
    "--include-prerelease",
    is_flag=True,
    default=None,
    help="Include prerelease versions",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def set_cmd(
    name: str,
    version: str | None,
    ref: str | None,
    description: str | None,
    subdir: str | None,
    tag_pattern: str | None,
    tags: str | None,
    include_prerelease: bool | None,
    verbose: bool,
) -> None:
    """Update fields on an existing plugin entry."""
    from ....marketplace.yml_editor import update_plugin_entry
    from ....marketplace.yml_schema import load_marketplace_yml

    logger = CommandLogger("marketplace-plugin-set", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    if version and ref:
        raise click.UsageError(
            "--version and --ref are mutually exclusive. "
            "Use --version for semver ranges or --ref for git refs."
        )

    if ref is not None and not _SHA_RE.match(ref):
        yml_data = load_marketplace_yml(yml)
        source: str | None = None
        for pkg in yml_data.packages:
            if pkg.name.lower() == name.lower():
                source = pkg.source
                break
        if source is None:
            logger.error(f"Package '{name}' not found", symbol="error")
            sys.exit(2)
        ref = _resolve_ref(logger, source, ref, version, no_verify=False)

    parsed_tags = _parse_tags(tags)

    fields: dict[str, object] = {}
    if version is not None:
        fields["version"] = version
    if ref is not None:
        fields["ref"] = ref
    if description is not None:
        fields["description"] = description
    if subdir is not None:
        fields["subdir"] = subdir
    if tag_pattern is not None:
        fields["tag_pattern"] = tag_pattern
    if parsed_tags is not None:
        fields["tags"] = parsed_tags
    if include_prerelease is not None:
        fields["include_prerelease"] = include_prerelease

    if not fields:
        logger.error(
            "No fields specified. Pass at least one option "
            "(e.g. --version, --ref, --description).",
            symbol="error",
        )
        sys.exit(1)

    try:
        update_plugin_entry(yml, name, **fields)
    except MarketplaceYmlError as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(2)

    logger.success(f"Updated plugin '{name}'", symbol="check")
