"""``apm marketplace check`` command."""

from __future__ import annotations

import sys
import traceback
from typing import TYPE_CHECKING

import click

from ...core.command_logger import CommandLogger
from ...marketplace.auth_helpers import resolve_auth_for_host
from ...marketplace.errors import GitLsRemoteError, OfflineMissError
from ...marketplace.ref_resolver import (
    RefResolver,
    explicit_ref_error,
    package_entry_coordinates,
)
from ...marketplace.semver import satisfies_range
from ...utils.github_host import default_host, is_azure_devops_hostname
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

    # One resolver per effective (host, org) pair. An entry whose source
    # named a non-default host -- a host-prefixed source or a relative source
    # composed onto ``marketplace.sourceBase`` -- must be resolved against
    # that host with the host's (and, for sourceBase, the org's) token,
    # exactly like ``apm pack`` does. Default-host entries resolve the
    # configured default host through the same authentication path.
    source_base = getattr(yml, "source_base", None)
    resolvers: dict[tuple[str | None, str | None], RefResolver] = {}
    auth_resolver: AuthResolver | None = None

    def _resolver_for(host: str | None, org: str | None) -> RefResolver:
        nonlocal auth_resolver
        resolved_host = host or default_host() or "github.com"
        key = (resolved_host, org)
        if key not in resolvers:
            if host is None and offline:
                resolvers[key] = RefResolver(offline=offline)
            else:
                if auth_resolver is None and not offline:
                    from ...core.auth import AuthResolver

                    auth_resolver = AuthResolver()
                auth = resolve_auth_for_host(
                    resolved_host,
                    offline=offline,
                    org=org,
                    auth_resolver=auth_resolver,
                )
                resolvers[key] = RefResolver(
                    offline=offline,
                    host=resolved_host,
                    token=auth.token if auth else None,
                    auth_scheme=auth.auth_scheme if auth else "basic",
                    git_env=(
                        auth_resolver.hardened_git_env_for_context(auth)
                        if auth_resolver is not None and auth is not None
                        else None
                    ),
                    auth_resolver=auth_resolver,
                    auth_target=resolved_host,
                )
        return resolvers[key]

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
                host, owner_repo, org, display_url = package_entry_coordinates(entry, source_base)
                if display_url is not None:
                    remote_label = display_url
                elif host and is_azure_devops_hostname(host):
                    remote_label = f"https://{host}/{owner_repo}"
                else:
                    remote_label = f"https://{host}/{owner_repo}.git" if host else owner_repo
                logger.verbose_detail(
                    f"Resolving {entry.name} via "
                    f"{host or default_host() or 'github.com'}: {remote_label}"
                )
                resolver = _resolver_for(host, org)
                refs = resolver.list_remote_refs(owner_repo)

                # Check version/ref resolution
                if entry.ref is not None:
                    error = explicit_ref_error(
                        entry.ref,
                        refs,
                        resolver=resolver,
                        owner_repo=owner_repo,
                        offline=offline,
                    )
                    if error:
                        results.append(
                            _CheckResult(
                                name=entry.name,
                                reachable=True,
                                version_found=False,
                                ref_ok=False,
                                error=error,
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
                    if not matching:
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
