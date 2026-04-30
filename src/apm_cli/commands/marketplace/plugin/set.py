"""``apm marketplace package set`` command."""

from __future__ import annotations

import sys

import click

from ....core.command_logger import CommandLogger
from ....marketplace.errors import MarketplaceYmlError
from . import (
    _SHA_RE,
    _ensure_yml_exists,
    _parse_tags,
    _resolve_ref,
    package,
)


@package.command("set", help="Update a package entry in marketplace authoring config")
@click.argument("name")
@click.option("--version", default=None, help="Semver range (e.g. '>=1.0.0')")
@click.option(
    "--ref",
    default=None,
    help="Pin to a git ref (SHA, tag, or HEAD). Mutable refs are auto-resolved to SHA.",
)
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
    name,
    version,
    ref,
    subdir,
    tag_pattern,
    tags,
    include_prerelease,
    verbose,
):
    """Update fields on an existing package entry."""
    from ....marketplace.yml_editor import update_plugin_entry

    logger = CommandLogger("marketplace-package-set", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    # --version and --ref are mutually exclusive.
    if version and ref:
        raise click.UsageError(
            "--version and --ref are mutually exclusive. "
            "Use --version for semver ranges or --ref for git refs."
        )

    # Resolve mutable refs to concrete SHAs.
    if ref is not None and not _SHA_RE.match(ref):
        from ....marketplace.yml_schema import (
            load_marketplace_from_apm_yml,
            load_marketplace_yml,
        )

        if yml.name == "apm.yml":
            yml_data = load_marketplace_from_apm_yml(yml)
        else:
            yml_data = load_marketplace_yml(yml)
        source = None
        for pkg in yml_data.packages:
            if pkg.name.lower() == name.lower():
                source = pkg.source
                break
        if source is None:
            logger.error(f"Package '{name}' not found", symbol="error")
            sys.exit(2)
        ref = _resolve_ref(logger, source, ref, version, no_verify=False)

    parsed_tags = _parse_tags(tags)

    fields = {}
    if version is not None:
        fields["version"] = version
    if ref is not None:
        fields["ref"] = ref
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
            "No fields specified. Pass at least one option (e.g. --version, --ref, --subdir).",
            symbol="error",
        )
        sys.exit(1)

    try:
        update_plugin_entry(yml, name, **fields)
    except MarketplaceYmlError as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(2)

    logger.success(f"Updated package '{name}'", symbol="check")
