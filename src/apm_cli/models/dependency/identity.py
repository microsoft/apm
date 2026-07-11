"""Dependency identity helpers -- key derivation, canonical strings, semver guards.

Extracted from ``reference.py`` to keep that module within the source
file-length guardrail. These are pure, stateless helpers with no dependency on
``DependencyReference`` internals; both ``DependencyReference`` and
``LockedDependency`` reuse them so the two identity models share one body shape
without collapsing their distinct local-detection semantics.

``normalize_package_repo_url`` is the single casing-normalization boundary for
package identity. Callers must not lowercase repository paths independently.
"""

import re

from ...utils.github_host import default_host, is_github_hostname

# Allowed character set for a single repository path segment.
#
# ADO accepts spaces (project / repo names can contain them) but NOT tilde --
# tilde has no meaning on Azure DevOps URLs and keeping it out preserves the
# asymmetry that protects the ADO surface from inadvertent regressions.
#
# Non-ADO hosts accept tilde because Bitbucket Data Center / Server (and
# Sourcehut) use ``~username`` path segments for personal repositories
# (e.g. ``/scm/~jdoe/repo.git``). ``~`` is RFC 3986 unreserved, has no
# POSIX path-traversal meaning, and all subprocess calls in APM use
# list-form ``argv`` so there is no shell-expansion vector.
_ADO_PATH_SEGMENT_RE = r"^[a-zA-Z0-9._\- ]+$"
_NON_ADO_PATH_SEGMENT_RE = r"^[a-zA-Z0-9._~-]+$"

_RANGE_PREFIX_RE = re.compile(r"^(>=|<=|>|<|\^|~|=)")


def normalize_package_repo_url(
    repo_url: str,
    *,
    host: str | None = None,
    source: str | None = None,
    registry_prefix: str | None = None,
    is_local: bool = False,
    is_marketplace: bool = False,
) -> str:
    """Return the canonical repository path for package identity.

    GitHub and package-registry identifiers are case-insensitive, so their
    owner/repository path is lowercase at the model boundary. Unknown git
    hosts retain their path casing because their repository semantics may be
    case-sensitive.
    """
    if is_local or source == "local" or is_marketplace:
        return repo_url

    if source == "registry" or registry_prefix:
        return repo_url.lower()

    configured_default_host = default_host()
    effective_host = host or configured_default_host
    if effective_host.lower() == configured_default_host.lower() or is_github_hostname(
        effective_host
    ):
        return repo_url.lower()
    return repo_url


def build_dependency_unique_key(
    repo_url: str,
    *,
    host: str | None = None,
    source: str | None = None,
    local_path: str | None = None,
    is_virtual: bool = False,
    virtual_path: str | None = None,
    registry_prefix: str | None = None,
    declaring_parent: str | None = None,
    anchored_local_path: str | None = None,
) -> str:
    """Return the lockfile/dedup key for a dependency identity.

    github.com remains the implicit default so existing lockfiles keep bare
    ``owner/repo`` keys. Non-default hosts include the host segment to avoid
    collisions between the same ``owner/repo`` on different servers.
    This deliberately uses the literal ``github.com`` default rather than
    environment-specific host overrides, so lockfile keys stay portable across
    machines with different GitHub Enterprise defaults.

    Registry-proxy deps (``registry_prefix`` set, e.g. an Artifactory mirror)
    keep the bare logical key: the proxy host is a transport detail, not the
    package identity, and the manifest side always declares the upstream
    ``owner/repo`` shorthand. Host-qualifying them would break the manifest /
    lockfile key correspondence used by re-install and orphan detection.
    """
    if source == "local" and local_path:
        if declaring_parent:
            anchor = anchored_local_path or local_path
            return f"local:{declaring_parent}:{anchor}"
        return local_path

    key = repo_url
    if is_virtual and virtual_path:
        key = f"{key}/{virtual_path}"

    if registry_prefix:
        return key

    host_value = (host or "").strip()
    normalized_host = host_value.lower()
    if normalized_host and normalized_host != "github.com":
        return f"{normalized_host}/{key}"
    return key


def build_canonical_dependency_string(
    repo_url: str,
    *,
    is_local: bool = False,
    local_path: str | None = None,
    is_virtual: bool = False,
    virtual_path: str | None = None,
) -> str:
    """Return the host-blind canonical string for filesystem / orphan matching.

    Host-blind by construction: it never prefixes the host, so it matches the
    host-blind ``apm_modules/`` layout. Use :func:`build_dependency_unique_key`
    for the host-qualified lockfile dedup key.

    Callers pass their own ``is_local`` signal -- ``DependencyReference``
    derives it from its ``is_local`` property while ``LockedDependency`` derives
    it from ``source == "local"`` -- so single-sourcing the body shape does not
    collapse the two identity models' distinct local-detection semantics.
    """
    if is_local and local_path:
        return local_path
    if is_virtual and virtual_path:
        return f"{repo_url}/{virtual_path}"
    return repo_url


def _path_segment_pattern(is_ado_host: bool) -> str:
    """Return the allowed-character regex for a single repo path segment."""
    return _ADO_PATH_SEGMENT_RE if is_ado_host else _NON_ADO_PATH_SEGMENT_RE


def _is_valid_registry_semver_range(spec: str) -> bool:
    """Defer importing ``deps.registry`` until call time (avoids import cycles)."""
    from ...deps.registry.semver import is_semver_range

    return is_semver_range(spec)


class InvalidSemverRangeError(ValueError):
    """Raised when a ref starts like a semver range but is invalid."""


def _looks_like_invalid_semver_range(spec: str) -> bool:
    """Return whether *spec* starts like a semver range but is invalid."""
    return bool(_RANGE_PREFIX_RE.match(spec.strip()))
