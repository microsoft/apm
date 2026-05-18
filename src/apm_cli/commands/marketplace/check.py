"""``apm marketplace check`` command."""

from __future__ import annotations

import sys
import traceback

import click

from ...core.command_logger import CommandLogger
from ...marketplace.errors import GitLsRemoteError, OfflineMissError
from ...marketplace.ref_resolver import RefResolver
from . import marketplace
from ._check import _CheckResult, _render_check_table, _warn_duplicate_names
from ._io import _load_config_or_exit


def _check_entry_against_refs(entry, refs, yml) -> tuple[bool, bool, str]:
    """Check a single entry against its remote refs.

    Return ``(ref_ok, version_found, error_msg)``.  When *error_msg* is
    non-empty the check failed and the caller should record a failure result.
    """
    from ...marketplace.semver import satisfies_range
    from ._outdated import _extract_tag_versions

    if entry.ref is not None:
        for r in refs:
            tag_name = r.name
            if tag_name.startswith("refs/tags/"):
                tag_name = tag_name[len("refs/tags/") :]
            elif tag_name.startswith("refs/heads/"):
                tag_name = tag_name[len("refs/heads/") :]
            if entry.ref in (tag_name, r.name):
                return True, True, ""
        return False, False, f"Ref '{entry.ref}' not found"

    tag_versions = _extract_tag_versions(refs, entry, yml, False)
    version_range = entry.version or ""
    matching = [(sv, tag) for sv, tag in tag_versions if satisfies_range(sv, version_range)]
    if matching:
        return True, True, ""
    return False, len(tag_versions) > 0, f"No tag matching '{version_range}'"


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

                ref_ok, version_found, error_msg = _check_entry_against_refs(entry, refs, yml)
                if error_msg:
                    results.append(
                        _CheckResult(
                            name=entry.name,
                            reachable=True,
                            version_found=version_found,
                            ref_ok=ref_ok,
                            error=error_msg,
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
