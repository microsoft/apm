"""Host-less shorthand under a manifest ``default_host:`` (OpenAPM req-mf-019).

Single owner for both directions of the override:

- Reading: a host-less shorthand is parsed exactly as ``<default_host>/<shorthand>``,
  so that host's repository-boundary and virtual-path rules (GitLab subgroups,
  Azure DevOps ``org/project/repo``) apply unchanged.
- Writing: the default host is dropped only when the stripped form is again a
  host-less shorthand, so ``parse(serialize(ref))`` keeps the same repository
  boundary and virtual path.

Without an override (or when it equals ``GITHUB_HOST`` / ``github.com``) both
directions keep the implementation-default behaviour.
"""

from ...cache.url_normalize import SCP_LIKE_RE
from ...utils.github_host import default_host, shorthand_default_host

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
