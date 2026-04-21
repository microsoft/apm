"""``apm marketplace plugin {add,set,remove}`` subgroup.

Lets maintainers programmatically manage package entries in
``marketplace.yml`` instead of hand-editing YAML.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ..core.command_logger import CommandLogger
from ..marketplace.errors import (
    GitLsRemoteError,
    MarketplaceYmlError,
    OfflineMissError,
)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _yml_path() -> Path:
    """Return the canonical ``marketplace.yml`` path in CWD."""
    return Path.cwd() / "marketplace.yml"


def _ensure_yml_exists(logger: CommandLogger) -> Path:
    """Return the yml path or exit with guidance if it does not exist."""
    path = _yml_path()
    if not path.exists():
        logger.error(
            "No marketplace.yml found. "
            "Run 'apm marketplace init' to scaffold one.",
            symbol="error",
        )
        sys.exit(1)
    return path


def _parse_tags(raw: str | None) -> list[str] | None:
    """Split a comma-separated tag string into a list, or return None."""
    if raw is None:
        return None
    parts = [t.strip() for t in raw.split(",") if t.strip()]
    return parts if parts else None


def _verify_source(logger: CommandLogger, source: str) -> None:
    """Run ``git ls-remote`` against *source* to verify reachability."""
    from ..marketplace.ref_resolver import RefResolver

    resolver = RefResolver()
    try:
        resolver.list_remote_refs(source)
    except GitLsRemoteError as exc:
        logger.error(
            f"Source '{source}' is not reachable: {exc}",
            symbol="error",
        )
        sys.exit(2)
    except OfflineMissError:
        logger.warning(
            f"Cannot verify source '{source}' (offline / no cache).",
            symbol="warning",
        )


# -------------------------------------------------------------------
# Click group
# -------------------------------------------------------------------


@click.group(help="Manage plugins in marketplace.yml (add, set, remove)")
def plugin():
    """Add, update, or remove packages in marketplace.yml."""
    pass


# -------------------------------------------------------------------
# plugin add
# -------------------------------------------------------------------


@plugin.command(help="Add a plugin to marketplace.yml")
@click.argument("source")
@click.option("--name", default=None, help="Package name (default: repo name)")
@click.option("--version", default=None, help="Semver range (e.g. '>=1.0.0')")
@click.option("--ref", default=None, help="Pin to SHA or tag")
@click.option("--description", default=None, help="Human-readable description")
@click.option("--subdir", default=None, help="Subdirectory inside source repo")
@click.option("--tag-pattern", default=None, help="Tag pattern (e.g. 'v{version}')")
@click.option("--tags", default=None, help="Comma-separated tags")
@click.option(
    "--include-prerelease", is_flag=True, help="Include prerelease versions"
)
@click.option("--no-verify", is_flag=True, help="Skip remote reachability check")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def add(
    source,
    name,
    version,
    ref,
    description,
    subdir,
    tag_pattern,
    tags,
    include_prerelease,
    no_verify,
    verbose,
):
    """Add a plugin entry to marketplace.yml."""
    from ..marketplace.yml_editor import add_plugin_entry

    logger = CommandLogger("marketplace-plugin-add", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    # --version and --ref are mutually exclusive.
    if version and ref:
        raise click.UsageError(
            "--version and --ref are mutually exclusive. "
            "Use --version for semver ranges or --ref for git refs."
        )

    parsed_tags = _parse_tags(tags)

    # Verify source reachability unless skipped.
    if not no_verify:
        _verify_source(logger, source)

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

    logger.success(
        f"Added plugin '{resolved_name}' from {source}",
        symbol="check",
    )


# -------------------------------------------------------------------
# plugin set
# -------------------------------------------------------------------


@plugin.command("set", help="Update a plugin entry in marketplace.yml")
@click.argument("name")
@click.option("--version", default=None, help="Semver range (e.g. '>=1.0.0')")
@click.option("--ref", default=None, help="Pin to SHA or tag")
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
    name,
    version,
    ref,
    description,
    subdir,
    tag_pattern,
    tags,
    include_prerelease,
    verbose,
):
    """Update fields on an existing plugin entry."""
    from ..marketplace.yml_editor import update_plugin_entry

    logger = CommandLogger("marketplace-plugin-set", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    parsed_tags = _parse_tags(tags)

    fields = {}
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


# -------------------------------------------------------------------
# plugin remove
# -------------------------------------------------------------------


@plugin.command(help="Remove a plugin from marketplace.yml")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def remove(name, yes, verbose):
    """Remove a plugin entry from marketplace.yml."""
    from ..marketplace.yml_editor import remove_plugin_entry

    logger = CommandLogger("marketplace-plugin-remove", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    # Confirmation gate.
    if not yes:
        try:
            click.confirm(
                f"Remove plugin '{name}' from marketplace.yml?",
                abort=True,
            )
        except click.Abort:
            click.echo("Cancelled.")
            return

    try:
        remove_plugin_entry(yml, name)
    except MarketplaceYmlError as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(2)

    logger.success(f"Removed plugin '{name}'", symbol="check")
