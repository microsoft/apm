"""``apm marketplace check`` command."""

from __future__ import annotations

import sys
import traceback

import click

from ...core.command_logger import CommandLogger
from ...marketplace.errors import GitLsRemoteError, OfflineMissError
from ...marketplace.ref_resolver import RefResolver
from ...marketplace.semver import satisfies_range
from . import (
    _CheckResult,
    _extract_tag_versions,
    _load_config_or_exit,
    _render_check_table,
    _warn_duplicate_names,
    marketplace,
)


@marketplace.command(help="Validate marketplace entries are resolvable")
@click.option("--offline", is_flag=True, help="Schema + cached-ref checks only (no network)")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def check(offline, verbose):
    """Validate marketplace.yml and check each entry is resolvable."""
    logger = CommandLogger("marketplace-check", verbose=verbose)

    _, yml = _load_config_or_exit(logger)

    # Defence-in-depth: flag duplicate package names (yml_schema
    # also rejects them, but an extra check keeps diagnostics visible).
    _warn_duplicate_names(logger, yml)

    if offline:
        logger.progress(
            "Offline mode -- only schema and cached-ref checks",
            symbol="info",
        )

    resolver = RefResolver(offline=offline)
    results = []
    failure_count = 0

    try:
        for entry in yml.packages:
            try:
                # Attempt to resolve each entry
                refs = resolver.list_remote_refs(entry.source)

                # Check version/ref resolution
                ref_ok = False
                if entry.ref is not None:
                    # Check the explicit ref exists
                    for r in refs:
                        tag_name = r.name
                        if tag_name.startswith("refs/tags/"):
                            tag_name = tag_name[len("refs/tags/") :]
                        elif tag_name.startswith("refs/heads/"):
                            tag_name = tag_name[len("refs/heads/") :]
                        if tag_name == entry.ref or r.name == entry.ref:  # noqa: PLR1714
                            ref_ok = True
                            break
                    if not ref_ok:
                        results.append(
                            _CheckResult(
                                name=entry.name,
                                reachable=True,
                                version_found=False,
                                ref_ok=False,
                                error=f"Ref '{entry.ref}' not found",
                            )
                        )
                        failure_count += 1
                        continue
                else:
                    # Version range -- check at least one tag satisfies
                    tag_versions = _extract_tag_versions(refs, entry, yml, False)
                    version_range = entry.version or ""
                    matching = [
                        (sv, tag) for sv, tag in tag_versions if satisfies_range(sv, version_range)
                    ]
                    if matching:
                        ref_ok = True
                    else:
                        results.append(
                            _CheckResult(
                                name=entry.name,
                                reachable=True,
                                version_found=len(tag_versions) > 0,
                                ref_ok=False,
                                error=f"No tag matching '{version_range}'",
                            )
                        )
                        failure_count += 1
                        continue

                results.append(
                    _CheckResult(
                        name=entry.name,
                        reachable=True,
                        version_found=True,
                        ref_ok=True,
                        error="",
                    )
                )

            except OfflineMissError:
                results.append(
                    _CheckResult(
                        name=entry.name,
                        reachable=False,
                        version_found=False,
                        ref_ok=False,
                        error="No cached refs (offline)",
                    )
                )
                failure_count += 1
            except GitLsRemoteError as exc:
                results.append(
                    _CheckResult(
                        name=entry.name,
                        reachable=False,
                        version_found=False,
                        ref_ok=False,
                        error=exc.summary_text[:60],
                    )
                )
                failure_count += 1
            except Exception as exc:
                results.append(
                    _CheckResult(
                        name=entry.name,
                        reachable=False,
                        version_found=False,
                        ref_ok=False,
                        error=str(exc)[:60],
                    )
                )
                failure_count += 1
                logger.verbose_detail(traceback.format_exc())

        _render_check_table(logger, results)

        total = len(results)
        if failure_count > 0:
            logger.error(f"{failure_count} entries have issues", symbol="error")
            sys.exit(1)
        else:
            logger.success(f"All {total} entries OK", symbol="check")

    finally:
        resolver.close()
