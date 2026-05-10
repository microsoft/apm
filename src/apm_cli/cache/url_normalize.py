"""URL normalization for content-addressable cache key derivation.

Produces deterministic cache keys by normalizing Git repository URLs
so that equivalent forms (HTTPS, SSH, with/without .git suffix, mixed
case hostnames) all map to the same shard.

Normalization steps
-------------------
1. Strip trailing ``.git``
2. Canonicalize ``git@host:path`` -> ``ssh://git@host/path``
3. Lowercase hostname (case-insensitive per RFC 3986)
4. Strip password from userinfo (keep username for protocol-required
   forms like ``git@``)
5. Strip default ports (``:443`` for https, ``:22`` for ssh)

The normalized string is then SHA-256 hashed (first 16 hex chars) to
produce a short, filesystem-safe shard key.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse

# SCP-like pattern: <user>@host:path (no scheme, colon separates host:path).
# Public symbol -- shared by `policy.discovery` and `models.dependency.reference`
# so dependency parsing and remote-URL introspection can never drift on what
# counts as an SCP-shorthand SSH URL (e.g. EMU users, Azure DevOps users).
SCP_LIKE_RE = re.compile(
    r"^(?P<user>[a-zA-Z0-9_][a-zA-Z0-9_.+-]*)@"
    r"(?P<host>[^:/]+)"
    r":(?P<path>.+)$"
)

# Default ports to strip
_DEFAULT_PORTS: dict[str, int] = {
    "https": 443,
    "ssh": 22,
    "http": 80,
    "git": 9418,
}


def normalize_repo_url(url: str) -> str:
    """Normalize a Git repository URL for cache key derivation.

    The result is a canonical string suitable for hashing. It is NOT
    necessarily a valid URL -- it is a deterministic representation.

    Args:
        url: Raw repository URL (HTTPS, SSH, SCP-like, or git://).

    Returns:
        Normalized URL string.

    Examples:
        >>> normalize_repo_url("https://github.com/Owner/Repo.git")
        'https://github.com/owner/repo'
        >>> normalize_repo_url("git@github.com:owner/repo.git")
        'ssh://git@github.com/owner/repo'
    """
    url = url.strip()

    # Step 2: Convert SCP-like to ssh:// form
    scp_match = SCP_LIKE_RE.match(url)
    if scp_match:
        user = scp_match.group("user")
        host = scp_match.group("host")
        path = scp_match.group("path")
        # Ensure path starts with /
        if not path.startswith("/"):
            path = "/" + path
        url = f"ssh://{user}@{host}{path}"

    # Parse the URL
    parsed = urllib.parse.urlparse(url)

    # Step 3: Lowercase hostname
    hostname = (parsed.hostname or "").lower()

    # Step 4: Strip password, keep username
    username = parsed.username or ""

    # Step 5: Strip default ports
    port = parsed.port
    scheme = (parsed.scheme or "https").lower()
    if port and _DEFAULT_PORTS.get(scheme) == port:
        port = None

    # Reconstruct the authority
    authority = f"{username}@{hostname}" if username else hostname
    if port:
        authority = f"{authority}:{port}"

    # Step 1: Strip trailing .git from path
    path = parsed.path or ""
    if path.endswith(".git"):
        path = path[:-4]

    # Lowercase path ONLY for hosts known to treat paths case-insensitively
    # (GitHub, GitLab.com, Bitbucket.org). Self-hosted Gitea and some
    # GitLab/ADO installs are case-sensitive on path components, where
    # collapsing case would risk cache-shard collisions across distinct
    # repositories.
    _CASE_INSENSITIVE_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}
    if hostname in _CASE_INSENSITIVE_HOSTS:
        path = path.lower()

    # Strip trailing slash from path
    path = path.rstrip("/")

    # Reconstruct normalized URL
    return f"{scheme}://{authority}{path}"


def cache_shard_key(url: str) -> str:
    """Derive a filesystem-safe shard key from a repository URL.

    Uses the first 16 hex characters of the SHA-256 hash of the
    normalized URL. This provides 2^-64 collision probability which
    is acceptable for local cache use, while keeping paths short
    (important for Windows path length limits).

    Args:
        url: Raw repository URL.

    Returns:
        16-character hex string suitable for use as a directory name.
    """
    normalized = normalize_repo_url(url)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:16]
