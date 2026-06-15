"""``apm marketplace check`` command."""

from __future__ import annotations

import sys
import traceback
from typing import TYPE_CHECKING

import click

from ...core.command_logger import CommandLogger
from ...marketplace.auth_helpers import resolve_token_for_host
from ...marketplace.errors import GitLsRemoteError, OfflineMissError
from ...marketplace.ref_resolver import RefResolver
from ...marketplace.semver import satisfies_range
from ...marketplace.yml_schema import PackageEntry, split_source_base
from . import (
    _CheckResult,
    _extract_tag_versions,
    _load_config_or_exit,
    _render_check_table,
    _warn_duplicate_names,
    marketplace,
)

if TYPE_CHECKING:
    from ...core.auth import AuthResolver


def _entry_coordinates(entry: PackageEntry, source_base: str | None) -> tuple[str | None, str]:
    """Return ``(host, owner_repo)`` for *entry*, mirroring the build-time
    routing in ``MarketplaceBuilder._remote_source_coordinates`` so that
    ``check`` and ``pack`` resolve every entry against the same host.

    - A per-entry host (``host.tld/owner/repo`` or full URL) is an override.
    - Otherwise, when ``marketplace.sourceBase`` is set, a host-less source
      composes onto the base.
    - Otherwise the source stays a default-host ``owner/repo``.
    """
    if entry.host:
        return entry.host, entry.source
    if source_base:
        base_host, base_path = split_source_base(source_base)
        return base_host, f"{base_path}/{entry.source}"
    return None, entry.source


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

    # One resolver per effective host. An entry whose source named a
    # non-default host -- a host-prefixed source or a relative source
    # composed onto ``marketplace.sourceBase`` -- must be resolved against
    # that host with the host's token, exactly like ``apm pack`` does.
    # Default-host entries keep the bare ambient-credential path.
    source_base = getattr(yml, "source_base", None)
    resolvers: dict[str | None, RefResolver] = {}
    auth_resolver: AuthResolver | None = None

    def _resolver_for(host: str | None) -> RefResolver:
        nonlocal auth_resolver
        if host not in resolvers:
            if host is None:
                resolvers[host] = RefResolver(offline=offline)
            else:
                if auth_resolver is None and not offline:
                    from ...core.auth import AuthResolver

                    auth_resolver = AuthResolver()
                token = resolve_token_for_host(
                    host,
                    offline=offline,
                    auth_resolver=auth_resolver,
                )
                resolvers[host] = RefResolver(offline=offline, host=host, token=token)
        return resolvers[host]

    results = []
    failure_count = 0

    try:
        for entry in yml.packages:
            # Local-path packages skip git resolution entirely.
            if entry.is_local:
                logger.verbose_detail(f"Skipping {entry.name} -- local path, no network check")
                results.append(
                    _CheckResult(
                        name=entry.name,
                        reachable=True,
                        version_found=True,
                        ref_ok=True,
                        error="",
                    )
                )
                continue
            try:
                # Resolve each entry against its effective host + composed path.
                host, owner_repo = _entry_coordinates(entry, source_base)
                logger.verbose_detail(
                    f"Resolving {entry.name} via {host or 'default host'}: {owner_repo}"
                )
                refs = _resolver_for(host).list_remote_refs(owner_repo)

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
        for resolver in resolvers.values():
            resolver.close()
