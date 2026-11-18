"""Host-less shorthand under a manifest ``default_host:`` (OpenAPM req-mf-019).

Single owner for both directions of the override:

- Reading: a host-less shorthand is parsed exactly as ``<default_host>/<shorthand>``,
  so that host's repository-boundary and virtual-path rules (GitLab subgroups,
  Azure DevOps ``org/project/repo``) apply unchanged.
- Writing: the default host is dropped only when the stripped form is again a
  host-less shorthand, so ``parse(serialize(ref))`` keeps the same repository
  boundary and virtual path.

- Scoping: :func:`manifest_host_scope_for` is the only way callers validate a
  manifest's ``default_host:`` and enter it, so install, uninstall, audit, and
  manifest parsing resolve shorthand identically.

Without an override (or when it equals ``GITHUB_HOST`` / ``github.com``) both
directions keep the implementation-default behaviour.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager

from ...cache.url_normalize import SCP_LIKE_RE
from ...utils.github_host import (
    default_host,
    manifest_default_host_scope,
    shorthand_default_host,
    validate_manifest_default_host,
)

_EXPLICIT_SOURCE_PREFIXES = ("git@", "https://", "http://", "ssh://", "/", ".", "~")


def _manifest_host_override() -> str | None:
    host = shorthand_default_host()
    return None if host.lower() == default_host().lower() else host


def is_hostless_shorthand(value: str) -> bool:
    """Return True for ``owner/repo[/...][#ref]`` with no host, scheme, or local path."""
    source = value.strip().split("#", 1)[0]
    if source.startswith(_EXPLICIT_SOURCE_PREFIXES) or "\\" in source:
        return False
    if SCP_LIKE_RE.match(source):
        return False
    parts = [part for part in source.split("/") if part]
    return len(parts) >= 2 and not any(char in parts[0] for char in ".:@")


def qualify_manifest_shorthand(dependency_str: str) -> str:
    """Prefix a host-less shorthand with the active manifest ``default_host:``."""
    host = _manifest_host_override()
    if host is None or not is_hostless_shorthand(dependency_str):
        return dependency_str
    return f"{host}/{dependency_str.strip()}"


def manifest_host_strips(host: str, stripped: str) -> bool:
    """Return True when *host* may be omitted from the apm.yml form *stripped*."""
    override = _manifest_host_override()
    if override is None:
        return host.lower() == default_host().lower()
    return host.lower() == override.lower() and is_hostless_shorthand(stripped)


class ManifestHostScope(AbstractContextManager):
    """Validated ``default_host:`` of one manifest; entering it scopes shorthand."""

    def __init__(self, host: str | None) -> None:
        self.host = host
        self._scope: AbstractContextManager | None = None

    def __enter__(self) -> str | None:
        self._scope = manifest_default_host_scope(self.host)
        self._scope.__enter__()
        return self.host

    def __exit__(self, *exc_info) -> bool | None:
        scope, self._scope = self._scope, None
        return scope.__exit__(*exc_info) if scope is not None else None


def manifest_host_scope_for(data: object) -> ManifestHostScope:
    """Validate *data*'s ``default_host:`` now and return a scope to enter later.

    *data* is a parsed apm.yml mapping (anything else means no override).
    Raises ``ValueError`` with a user-facing message when the value is not a
    bare hostname; callers own how that error is reported.
    """
    value = data.get("default_host") if isinstance(data, Mapping) else None
    return ManifestHostScope(validate_manifest_default_host(value))
