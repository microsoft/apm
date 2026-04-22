"""``apm marketplace outdated`` command."""

from __future__ import annotations

import sys
import traceback

import click

from ...core.command_logger import CommandLogger
from ...marketplace.errors import BuildError
from ...marketplace.semver import satisfies_range
from . import (
    marketplace,
    _OutdatedRow,
    _extract_tag_versions,
    _load_current_versions,
    _load_yml_or_exit,
    _render_outdated_table,
)


@marketplace.command(help="Show packages with available upgrades")
@click.option("--offline", is_flag=True, help="Use cached refs only (no network)")
@click.option(
    "--include-prerelease",
    is_flag=True,
    help="Include prerelease versions",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def outdated(offline: bool, include_prerelease: bool, verbose: bool) -> None:
    """Compare installed versions against latest available tags."""
    from . import RefResolver

    logger = CommandLogger("marketplace-outdated", verbose=verbose)

    yml = _load_yml_or_exit(logger)
    current_versions = _load_current_versions()

    resolver = RefResolver(offline=offline)
    try:
        rows: list[_OutdatedRow] = []
        upgradable = 0
        up_to_date = 0
        for entry in yml.packages:
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

            in_range = [
                (sv, tag)
                for sv, tag in tag_versions
                if satisfies_range(sv, version_range)
            ]
            latest_overall_sv, latest_overall_tag = max(tag_versions, key=lambda x: x[0])
            latest_in_range_tag = "--"
            if in_range:
                _, latest_in_range_tag = max(in_range, key=lambda x: x[0])

            current = current_versions.get(entry.name, "--")

            if current == latest_in_range_tag:
                status = "[+]"
                up_to_date += 1
            elif latest_in_range_tag != "--" and current != latest_in_range_tag:
                status = "[!]"
                upgradable += 1
            else:
                status = "[!]"
                upgradable += 1

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

        logger.progress(f"{upgradable} outdated, {up_to_date} up to date", symbol="info")

        if verbose:
            logger.verbose_detail(f"    {upgradable} upgradable entries")

        if upgradable > 0:
            sys.exit(1)

    except SystemExit:
        raise
    except Exception as exc:
        logger.error(f"Failed to check outdated packages: {exc}", symbol="error")
        if verbose:
            click.echo(traceback.format_exc(), err=True)
        sys.exit(1)
    finally:
        resolver.close()
