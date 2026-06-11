"""Version checking and update notification utilities."""

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from ..bootstrap_mirror import (
    get_release_metadata_url,
    release_metadata_public_lookup_blocked,
)
from ..core.auth import AuthResolver

_DEFAULT_REPO = "microsoft/apm"
_PUBLIC_GITHUB_URL = "https://github.com"
_PUBLIC_API_BASE = "https://api.github.com"
_VERSION_CHECK_AUTH_RESOLVER: AuthResolver | None = None


def _get_air_gap_github_url() -> str:
    """Return GITHUB_URL env var (stripped of trailing slash), or the public GitHub URL."""
    return os.environ.get("GITHUB_URL", _PUBLIC_GITHUB_URL).rstrip("/")


def _get_air_gap_repo() -> str:
    """Return APM_REPO env var, or the default microsoft/apm repository."""
    return os.environ.get("APM_REPO", _DEFAULT_REPO)


def _get_air_gap_version() -> str | None:
    """Return VERSION env var if set and non-empty, otherwise None."""
    v = os.environ.get("VERSION", "")
    return v if v else None


def _build_releases_api_url(
    github_url: str, repo: str, release_metadata_url: str | None = None
) -> str:
    """Build the release metadata URL for the given host and repository.

    ``APM_RELEASE_METADATA_URL`` wins when configured so enterprise mirrors can
    publish a static ``latest.json`` without emulating the GitHub API path. For
    public GitHub, targets api.github.com directly. For GitHub Enterprise Server
    (any other GITHUB_URL value), uses the /api/v3 prefix on the configured host.
    """
    if release_metadata_url is not None:
        return release_metadata_url
    if github_url == _PUBLIC_GITHUB_URL:
        return f"{_PUBLIC_API_BASE}/repos/{repo}/releases/latest"
    return f"{github_url}/api/v3/repos/{repo}/releases/latest"


def _get_version_check_auth_resolver() -> AuthResolver:
    """Return the reusable resolver for non-blocking version checks."""
    global _VERSION_CHECK_AUTH_RESOLVER
    if _VERSION_CHECK_AUTH_RESOLVER is None:
        _VERSION_CHECK_AUTH_RESOLVER = AuthResolver(allow_external_fallback=False)
    else:
        _VERSION_CHECK_AUTH_RESOLVER.clear_cache()
    return _VERSION_CHECK_AUTH_RESOLVER


def _reset_version_check_auth_resolver_for_tests() -> None:
    """Reset the cached version-check resolver for isolated unit tests."""
    global _VERSION_CHECK_AUTH_RESOLVER
    _VERSION_CHECK_AUTH_RESOLVER = None


def _get_github_token(github_url: str | None = None, repo: str | None = None) -> str | None:
    """Return a GitHub token through AuthResolver, or None.

    Version checks only need environment-scoped tokens. Disabling external
    fallback avoids invoking gh or git credential helpers from the non-blocking
    startup update check while keeping the token precedence centralized.
    """
    parsed = urlparse(github_url or _get_air_gap_github_url())
    host = parsed.hostname or "github.com"
    effective_repo = repo or _get_air_gap_repo()
    org = effective_repo.split("/", 1)[0] if "/" in effective_repo else None
    context = _get_version_check_auth_resolver().resolve(host, org=org)
    return context.token


def get_latest_version_from_github(repo: str | None = None, timeout: int = 2) -> str | None:
    """Fetch the latest release version from GitHub or a configured mirror.

    Respects the following environment variables (matching install.sh semantics):
      - ``VERSION``: when set, the API call is skipped entirely and the pinned
        version is returned directly.  Required for fully air-gapped setups.
      - ``APM_RELEASE_METADATA_URL``: exact mirror URL for release metadata.
      - ``APM_NO_DIRECT_FALLBACK``: when set to ``1``/``true``/``yes``/``on``,
        public GitHub metadata is not queried unless a mirror URL or ``VERSION``
        is configured.
      - ``GITHUB_URL``: base URL of the GitHub host (default
        ``https://github.com``).  A non-default value is treated as a GitHub
        Enterprise Server instance and the API is addressed at
        ``{GITHUB_URL}/api/v3``.
      - ``APM_REPO``: repository in ``owner/repo`` form (default
        ``microsoft/apm``).

    Also sends an Authorization header when a GitHub token is present in the
    environment (GITHUB_APM_PAT > GITHUB_TOKEN > GH_TOKEN) and no metadata
    mirror is configured, falling back to anonymous when none is set.  The token
    value is never logged or echoed.

    Args:
        repo: Repository override in ``owner/repo`` form.  When *None* (the
            default), the value of ``APM_REPO`` env var is used, falling back
            to ``microsoft/apm``.
        timeout: Request timeout in seconds (default: 2 for non-blocking).

    Returns:
        Version string (e.g., ``"0.6.3"``) or ``None`` if unable to fetch.
    """
    # When VERSION is pinned, skip the network call entirely.
    pinned = _get_air_gap_version()
    if pinned is not None:
        tag = pinned.lstrip("v")
        if re.match(r"^\d+\.\d+\.\d+(a\d+|b\d+|rc\d+)?$", tag):
            return tag
        return None

    try:
        import requests
    except ImportError:
        return None

    try:
        effective_repo = _get_air_gap_repo() if repo is None else repo
        github_url = _get_air_gap_github_url()
        release_metadata_url = get_release_metadata_url()
        if release_metadata_public_lookup_blocked(github_url):
            return None
        url = _build_releases_api_url(github_url, effective_repo, release_metadata_url)
        token = _get_github_token(github_url, effective_repo)
        headers = (
            {"Authorization": f"token {token}"} if token and release_metadata_url is None else {}
        )
        response = requests.get(url, headers=headers, timeout=timeout)

        if response.status_code != 200:
            return None

        data = response.json()
        tag_name = data.get("tag_name", "")

        # Strip 'v' prefix if present (e.g., "v0.6.3" -> "0.6.3")
        if tag_name.startswith("v"):
            tag_name = tag_name[1:]

        # Validate version format
        if re.match(r"^\d+\.\d+\.\d+(a\d+|b\d+|rc\d+)?$", tag_name):
            return tag_name

        return None
    except Exception:
        # Silently fail for any network/parsing errors
        return None


def parse_version(version_str: str) -> tuple[int, int, int, str] | None:
    """
    Parse a semantic version string into components.

    Args:
        version_str: Version string like "0.6.3" or "0.7.0a1"

    Returns:
        Tuple of (major, minor, patch, prerelease) or None if invalid
        prerelease is empty string for stable releases
    """
    # Match version pattern: major.minor.patch[prerelease]
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(a\d+|b\d+|rc\d+)?$", version_str)
    if not match:
        return None

    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3))
    prerelease = match.group(4) or ""

    return (major, minor, patch, prerelease)


def is_newer_version(current: str, latest: str) -> bool:
    """
    Compare two semantic versions.

    Args:
        current: Current version string
        latest: Latest version string

    Returns:
        True if latest is newer than current
    """
    current_parts = parse_version(current)
    latest_parts = parse_version(latest)

    # If either version is invalid, assume no update needed
    if not current_parts or not latest_parts:
        return False

    curr_maj, curr_min, curr_patch, curr_pre = current_parts
    lat_maj, lat_min, lat_patch, lat_pre = latest_parts

    # Compare major.minor.patch
    if (lat_maj, lat_min, lat_patch) > (curr_maj, curr_min, curr_patch):
        return True

    if (lat_maj, lat_min, lat_patch) < (curr_maj, curr_min, curr_patch):
        return False

    # Same major.minor.patch - compare prerelease
    # Stable releases (no prerelease) are newer than prereleases
    if not lat_pre and curr_pre:
        return True

    if lat_pre and not curr_pre:
        return False

    # Both have prereleases - compare them lexicographically
    # This handles a1 < a2 < b1 < rc1, etc.
    return lat_pre > curr_pre


def get_update_cache_path() -> Path:
    """Get path to version update cache file."""
    # Use a cache directory in user's home
    if sys.platform == "win32":
        cache_dir = Path.home() / "AppData" / "Local" / "apm" / "cache"
    else:
        # Unix-like systems (macOS, Linux)
        cache_dir = Path.home() / ".cache" / "apm"

    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "last_version_check"


def should_check_for_updates() -> bool:
    """
    Determine if we should check for updates based on cache.

    Checks at most once per day to avoid slowing down CLI.

    Returns:
        True if we should check for updates
    """
    try:
        cache_path = get_update_cache_path()

        if not cache_path.exists():
            return True

        # Check file age
        import time

        file_age_seconds = time.time() - cache_path.stat().st_mtime

        # Check once per day (86400 seconds)
        return file_age_seconds > 86400
    except Exception:
        # If any error, allow check
        return True


def save_version_check_timestamp():
    """Save timestamp of last version check to cache."""
    try:
        cache_path = get_update_cache_path()
        cache_path.touch()
    except Exception:
        # Silently fail if unable to save
        pass


def check_for_updates(current_version: str) -> str | None:
    """
    Check if a newer version is available.

    This function is designed to be non-blocking and cache-aware.

    Args:
        current_version: Current installed version

    Returns:
        Latest version string if update available, None otherwise
    """
    # Skip check if done recently
    if not should_check_for_updates():
        return None

    # Fetch latest version from GitHub
    latest_version = get_latest_version_from_github()

    # Save check timestamp regardless of result
    save_version_check_timestamp()

    if not latest_version:
        return None

    # Compare versions
    if is_newer_version(current_version, latest_version):
        return latest_version

    return None
