"""``apm marketplace outdated`` command."""

from __future__ import annotations

import sys
import traceback

import click

from ...core.command_logger import CommandLogger
from ...marketplace.errors import BuildError
from ...marketplace.ref_resolver import RefResolver
from ...marketplace.semver import satisfies_range
from . import marketplace
from ._io import _load_config_or_exit
from ._outdated import (
    _extract_tag_versions,
    _load_current_versions,
    _OutdatedRow,
    _render_outdated_table,
)


def _build_outdated_row(entry, current_versions, resolver, include_prerelease, yml):
    """Build an ``_OutdatedRow`` for *entry*; return ``(row, category)``.

    *category* is one of ``"skip"``, ``"error"``, ``"no_tags"``,
    ``"up_to_date"``, or ``"upgradable"``.
    """
    current = current_versions.get(entry.name, "--")

    if entry.ref is not None:
        return _OutdatedRow(
            name=entry.name,
            current=current,
            range_spec="--",
            latest_in_range="--",
            latest_overall="--",
            status="[i]",
            note="Pinned to ref; skipped",
        ), "skip"

    version_range = entry.version or ""
    if not version_range:
        return _OutdatedRow(
            name=entry.name,
            current=current,
            range_spec="--",
            latest_in_range="--",
            latest_overall="--",
            status="[i]",
            note="No version range",
        ), "skip"

    try:
        refs = resolver.list_remote_refs(entry.source)
    except (BuildError, Exception) as exc:
        return _OutdatedRow(
            name=entry.name,
            current=current,
            range_spec=version_range,
            latest_in_range="--",
            latest_overall="--",
            status="[x]",
            note=str(exc)[:60],
        ), "error"

    tag_versions = _extract_tag_versions(refs, entry, yml, include_prerelease)

    if not tag_versions:
        return _OutdatedRow(
            name=entry.name,
            current=current,
            range_spec=version_range,
            latest_in_range="--",
            latest_overall="--",
            status="[!]",
            note="No matching tags found",
        ), "no_tags"

    in_range = [(sv, tag) for sv, tag in tag_versions if satisfies_range(sv, version_range)]
    _latest_overall_sv, latest_overall_tag = max(tag_versions, key=lambda x: x[0])
    latest_in_range_tag = "--"
    if in_range:
        _, latest_in_range_tag = max(in_range, key=lambda x: x[0])

    if current == latest_in_range_tag:
        status = "[+]"
        category = "up_to_date"
    else:
        status = "[!]"
        category = "upgradable"

    if latest_overall_tag != latest_in_range_tag:
        status = "[*]"

    return _OutdatedRow(
        name=entry.name,
        current=current,
        range_spec=version_range,
        latest_in_range=latest_in_range_tag,
        latest_overall=latest_overall_tag,
        status=status,
        note="",
    ), category


@marketplace.command(help="Show packages with available upgrades")
@click.option("--offline", is_flag=True, help="Use cached refs only (no network)")
@click.option("--include-prerelease", is_flag=True, help="Include prerelease versions")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def outdated(offline, include_prerelease, verbose):
    """Compare installed versions against latest available tags."""
    logger = CommandLogger("marketplace-outdated", verbose=verbose)

    _, yml = _load_config_or_exit(logger)

    # Load current marketplace.json for "Current" column
    current_versions = _load_current_versions()

    resolver = RefResolver(offline=offline)
    try:
        rows = []
        upgradable = 0
        up_to_date = 0
        for entry in yml.packages:
            row, category = _build_outdated_row(
                entry, current_versions, resolver, include_prerelease, yml
            )
            rows.append(row)
            if category == "up_to_date":
                up_to_date += 1
            elif category == "upgradable":
                upgradable += 1

        _render_outdated_table(logger, rows)

        if upgradable > 0:
            logger.progress(
                f"{upgradable} package(s) can be updated",
                symbol="info",
            )
        else:
            logger.progress(
                "All packages are up to date",
                symbol="info",
            )

        if verbose:
            logger.verbose_detail(f"    {upgradable} upgradable entries")

        if upgradable > 0:
            sys.exit(1)
        sys.exit(0)

    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"Failed to check outdated packages: {e}", symbol="error")
        logger.verbose_detail(traceback.format_exc())
        sys.exit(1)
    finally:
        resolver.close()
