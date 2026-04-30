"""Version checking and update notification utilities."""

import re
import sys
from pathlib import Path
from typing import Optional, Tuple  # noqa: F401, UP035


def get_latest_version_from_github(repo: str = "microsoft/apm", timeout: int = 2) -> str | None:
    """
    Fetch the latest release version from GitHub API.

    Args:
        repo: Repository in format "owner/repo"
        timeout: Request timeout in seconds (default: 2 for non-blocking)

    Returns:
        Version string (e.g., "0.6.3") or None if unable to fetch
    """
    try:
        import requests
    except ImportError:
        return None

    try:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        response = requests.get(url, timeout=timeout)

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
