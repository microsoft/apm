"""``apm marketplace outdated`` command."""

from __future__ import annotations

import sys
import traceback

import click

from ...core.command_logger import CommandLogger
from ...marketplace.errors import BuildError
from ...marketplace.ref_resolver import RefResolver
from ...marketplace.semver import satisfies_range
from . import (
    _extract_tag_versions,
    _load_config_or_exit,
    _load_current_versions,
    _OutdatedRow,
    _render_outdated_table,
    marketplace,
)


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
            # Entries with explicit ref (no range) are skipped
            if entry.ref is not None:
                rows.append(
                    _OutdatedRow(
                        name=entry.name,
                        current=current_versions.get(entry.name, "--"),
                        range_spec="--",
                        latest_in_range="--",
                        latest_overall="--",
                        status="[i]",
                        note="Pinned to ref; skipped",
                    )
                )
                continue

            version_range = entry.version or ""
            if not version_range:
                rows.append(
                    _OutdatedRow(
                        name=entry.name,
                        current=current_versions.get(entry.name, "--"),
                        range_spec="--",
                        latest_in_range="--",
                        latest_overall="--",
                        status="[i]",
                        note="No version range",
                    )
                )
                continue

            try:
                refs = resolver.list_remote_refs(entry.source)
            except (BuildError, Exception) as exc:
                rows.append(
                    _OutdatedRow(
                        name=entry.name,
                        current=current_versions.get(entry.name, "--"),
                        range_spec=version_range,
                        latest_in_range="--",
                        latest_overall="--",
                        status="[x]",
                        note=str(exc)[:60],
                    )
                )
                continue

            # Parse tags into semvers
            tag_versions = _extract_tag_versions(refs, entry, yml, include_prerelease)

            if not tag_versions:
                rows.append(
                    _OutdatedRow(
                        name=entry.name,
                        current=current_versions.get(entry.name, "--"),
                        range_spec=version_range,
                        latest_in_range="--",
                        latest_overall="--",
                        status="[!]",
                        note="No matching tags found",
                    )
                )
                continue

            # Find highest in-range and highest overall
            in_range = [(sv, tag) for sv, tag in tag_versions if satisfies_range(sv, version_range)]
            latest_overall_sv, latest_overall_tag = max(tag_versions, key=lambda x: x[0])  # noqa: RUF059
            latest_in_range_tag = "--"
            if in_range:
                _, latest_in_range_tag = max(in_range, key=lambda x: x[0])

            current = current_versions.get(entry.name, "--")

            # Determine status
            if current == latest_in_range_tag:
                status = "[+]"
                up_to_date += 1
            elif latest_in_range_tag != "--" and current != latest_in_range_tag:  # noqa: PLR1714
                status = "[!]"
                upgradable += 1
            else:
                status = "[!]"
                upgradable += 1

            # Check if major upgrade available outside range
            if latest_overall_tag != latest_in_range_tag:
                status = "[*]"

            rows.append(
                _OutdatedRow(
                    name=entry.name,
                    current=current,
                    range_spec=version_range,
                    latest_in_range=latest_in_range_tag,
                    latest_overall=latest_overall_tag,
                    status=status,
                    note="",
                )
            )

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
