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


def _resolve_package_ref(logger, yml, name: str, ref: str | None, version: str | None):
    """Resolve mutable refs to immutable SHAs for marketplace updates."""
    if ref is None or _SHA_RE.match(ref):
        return ref

    from ....marketplace.yml_schema import load_marketplace_from_apm_yml, load_marketplace_yml

    yml_data = (
        load_marketplace_from_apm_yml(yml) if yml.name == "apm.yml" else load_marketplace_yml(yml)
    )
    for pkg in yml_data.packages:
        if pkg.name.lower() == name.lower():
            return _resolve_ref(logger, pkg.source, ref, version, no_verify=False)

    logger.error(f"Package '{name}' not found", symbol="error")
    sys.exit(2)


def _build_update_fields(values: dict) -> dict:
    """Collect only the explicitly requested field updates."""
    fields = {}
    for key in ("version", "ref", "subdir", "tag_pattern", "include_prerelease"):
        value = values.get(key)
        if value is not None:
            fields[key] = value
    if values.get("parsed_tags") is not None:
        fields["tags"] = values["parsed_tags"]
    return fields


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
def set_cmd(name, **params):
    """Update fields on an existing package entry."""
    from ....marketplace.yml_editor import update_plugin_entry

    version = params["version"]
    ref = params["ref"]
    subdir = params["subdir"]
    tag_pattern = params["tag_pattern"]
    tags = params["tags"]
    include_prerelease = params["include_prerelease"]
    verbose = params["verbose"]
    logger = CommandLogger("marketplace-package-set", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    # --version and --ref are mutually exclusive.
    if version and ref:
        raise click.UsageError(
            "--version and --ref are mutually exclusive. "
            "Use --version for semver ranges or --ref for git refs."
        )

    ref = _resolve_package_ref(logger, yml, name, ref, version)
    parsed_tags = _parse_tags(tags)
    fields = _build_update_fields(
        {
            "version": version,
            "ref": ref,
            "subdir": subdir,
            "tag_pattern": tag_pattern,
            "parsed_tags": parsed_tags,
            "include_prerelease": include_prerelease,
        }
    )

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
