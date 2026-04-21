"""HTTP dependency policy helpers shared by install command and pipeline."""

import sys
import urllib.parse
from dataclasses import dataclass

import click

from ..utils.console import _rich_error, _rich_warning
from ..utils.github_host import is_valid_fqdn


@dataclass(frozen=True)
class _InsecureDependencyInfo:
    """Resolved insecure dependency details for warnings and consent checks."""

    url: str
    is_transitive: bool
    introduced_by: str | None = None


def _collect_insecure_dependency_infos(
    deps_to_install, dependency_graph
) -> list[_InsecureDependencyInfo]:
    """Collect insecure dependency details from the resolved install set."""
    insecure_infos: list[_InsecureDependencyInfo] = []
    tree = dependency_graph.dependency_tree

    for dep in deps_to_install:
        if getattr(dep, "is_insecure", False) is not True:
            continue

        node = tree.get_node(dep.get_unique_key()) if tree else None
        parent = node.parent if node else None
        insecure_infos.append(
            _InsecureDependencyInfo(
                url=_get_insecure_dependency_url(dep),
                is_transitive=parent is not None,
                introduced_by=(
                    parent.dependency_ref.get_display_name()
                    if parent is not None
                    else None
                ),
            )
        )

    return insecure_infos


def _get_insecure_dependency_url(dep) -> str:
    """Return the transport-aware display URL for an insecure dependency."""
    entry = dep.to_apm_yml_entry()
    if not isinstance(entry, dict):
        return entry

    url = entry["git"]
    if entry.get("ref"):
        return f"{url}#{entry['ref']}"
    return url


def _format_insecure_dependency_requirements(url: str) -> str:
    """Render the canonical remediation message for an HTTP dependency."""
    return (
        f"{url} -- HTTP dependency (no transport encryption)\n"
        "To install:\n"
        "  1. Set allow_insecure: true on the dep in apm.yml\n"
        "  2. Pass --allow-insecure to apm install"
    )


def _format_insecure_dependency_warning(info: _InsecureDependencyInfo) -> str:
    """Render the install-time warning text for an insecure dependency."""
    message = f"Fetching insecurely (no transport auth): {info.url}"
    if info.is_transitive and info.introduced_by:
        message = (
            f"{message} (transitive, introduced by {info.introduced_by})"
        )
    return message


def _warn_insecure_dependencies(insecure_infos, logger=None) -> None:
    """Emit one warning per insecure dependency before fetch begins."""
    for info in insecure_infos:
        message = _format_insecure_dependency_warning(info)
        if logger:
            logger.warning(message)
        else:
            _rich_warning(message)


def _normalize_allow_insecure_host(hostname: str) -> str:
    """Validate and normalize a hostname passed via --allow-insecure-host."""
    normalized = hostname.strip().lower()
    if not is_valid_fqdn(normalized):
        raise ValueError(
            f"Invalid hostname '{hostname}'. Use a bare hostname like 'mirror.example.com'."
        )
    return normalized


def _allow_insecure_host_callback(ctx, param, value):
    """Normalize repeatable --allow-insecure-host values for Click."""
    normalized_hosts = []
    seen_hosts = set()
    for raw_host in value or ():
        try:
            normalized = _normalize_allow_insecure_host(raw_host)
        except ValueError as exc:
            raise click.BadParameter(str(exc))
        if normalized not in seen_hosts:
            seen_hosts.add(normalized)
            normalized_hosts.append(normalized)
    return tuple(normalized_hosts)


def _get_insecure_dependency_host(info: _InsecureDependencyInfo) -> str | None:
    """Extract the hostname from an insecure dependency warning record."""
    parsed = urllib.parse.urlparse(info.url)
    return parsed.hostname.lower() if parsed.hostname else None


def _get_allowed_transitive_insecure_hosts(
    insecure_infos,
    *,
    allow_insecure: bool,
    allow_insecure_hosts,
) -> set[str]:
    """Build the hostname allowlist for transitive insecure dependencies."""
    allowed_hosts = set(allow_insecure_hosts)
    if not allow_insecure:
        return allowed_hosts

    for info in insecure_infos:
        if info.is_transitive:
            continue
        host = _get_insecure_dependency_host(info)
        if host:
            allowed_hosts.add(host)
    return allowed_hosts


def _guard_transitive_insecure_dependencies(
    insecure_infos,
    *,
    allow_insecure: bool,
    allow_insecure_hosts=(),
    logger=None,
) -> None:
    """Block transitive insecure dependencies from unapproved hosts."""
    transitive_infos = [info for info in insecure_infos if info.is_transitive]
    if not transitive_infos:
        return

    allowed_hosts = _get_allowed_transitive_insecure_hosts(
        insecure_infos,
        allow_insecure=allow_insecure,
        allow_insecure_hosts=allow_insecure_hosts,
    )
    blocked_hosts = sorted(
        {
            host
            for host in (
                _get_insecure_dependency_host(info) for info in transitive_infos
            )
            if host and host not in allowed_hosts
        }
    )
    if not blocked_hosts:
        return

    suggested_flags = " ".join(
        f"--allow-insecure-host {host}" for host in blocked_hosts
    )
    message = (
        "Transitive HTTP (insecure) dependencies were found on unapproved host(s): "
        f"{', '.join(blocked_hosts)}. "
        "--allow-insecure only covers direct HTTP dependencies and transitive "
        "HTTP dependencies on the same host. "
        f"Re-run with {suggested_flags} to allow these transitive hosts."
    )
    if logger:
        logger.error(message)
    else:
        _rich_error(message)
    sys.exit(1)


def _check_insecure_dependencies(
    deps, allow_insecure_flag: bool, logger=None
) -> None:
    """Check direct APM dependencies for HTTP (insecure) URLs and enforce policy.

    Two conditions must BOTH be true for an HTTP dep to be allowed:
    1. The dep entry in apm.yml must have allow_insecure: true
    2. --allow-insecure must be set for this install invocation

    Args:
        deps: List of DependencyReference objects to check.
        allow_insecure_flag: True if --allow-insecure was passed on the command line.
    """
    for dep in deps:
        dep_is_insecure = getattr(dep, "is_insecure", False) is True
        if not dep_is_insecure:
            continue
        url = _get_insecure_dependency_url(dep)
        dep_allow_insecure = getattr(dep, "allow_insecure", False) is True
        if not dep_allow_insecure:
            message = _format_insecure_dependency_requirements(url)
            if logger:
                logger.error(message)
            else:
                _rich_error(message)
            sys.exit(1)
        if not allow_insecure_flag:
            message = _format_insecure_dependency_requirements(url)
            if logger:
                logger.error(message)
            else:
                _rich_error(message)
            sys.exit(1)
