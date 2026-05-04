"""Cache root resolution and escape hatch handling.

Resolves the platform-appropriate cache directory following standard
conventions:
- Unix: ``${XDG_CACHE_HOME:-$HOME/.cache}/apm/``
- macOS: ``$HOME/Library/Caches/apm/`` (or XDG if explicitly set)
- Windows: ``%LOCALAPPDATA%\\apm\\Cache\\``

Escape hatches
--------------
- ``APM_CACHE_DIR=/path``: Override cache root entirely.
- ``APM_NO_CACHE=1``: Use a per-invocation temp directory (cleaned at exit).
- ``--refresh`` flag (handled by caller): Force revalidation on cache hit.

Precedence: ``--no-cache`` > ``APM_NO_CACHE`` > ``APM_CACHE_DIR`` > default.

Security
--------
- Cache root validated: must be absolute (after ~ expansion), no NUL bytes.
- Directories created with mode 0o700.
- Path validated via ``ensure_path_within`` before any shard access.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
from pathlib import Path

_log = logging.getLogger(__name__)

# Bucket layout within cache root
GIT_DB_BUCKET = "git/db_v1"
GIT_CHECKOUTS_BUCKET = "git/checkouts_v1"
HTTP_BUCKET = "http_v1"

# Temp cache dir (for APM_NO_CACHE mode) -- cleaned at process exit
_temp_cache_dir: str | None = None


def get_cache_root(*, no_cache: bool = False) -> Path:
    """Resolve the cache root directory.

    Args:
        no_cache: If True, returns a temporary directory that will be
            cleaned up at process exit (APM_NO_CACHE mode).

    Returns:
        Path to the cache root directory (created with mode 0o700 if
        it does not exist).

    Raises:
        ValueError: If the resolved path is invalid (contains NUL bytes,
            is empty after expansion).
    """
    # Escape hatch: APM_NO_CACHE or explicit no_cache flag
    if no_cache or os.environ.get("APM_NO_CACHE", "").strip() in ("1", "true", "yes"):
        return _get_temp_cache_root()

    # Escape hatch: APM_CACHE_DIR override
    override = os.environ.get("APM_CACHE_DIR", "").strip()
    if override:
        return _validate_and_ensure(override)

    # Platform default
    return _validate_and_ensure(_platform_default())


def get_git_db_path(cache_root: Path) -> Path:
    """Return the git database bucket path (full clones)."""
    return cache_root / GIT_DB_BUCKET


def get_git_checkouts_path(cache_root: Path) -> Path:
    """Return the git checkouts bucket path (per-SHA working copies)."""
    return cache_root / GIT_CHECKOUTS_BUCKET


def get_http_path(cache_root: Path) -> Path:
    """Return the HTTP cache bucket path."""
    return cache_root / HTTP_BUCKET


def _platform_default() -> str:
    """Return the platform-specific default cache path string."""
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            return os.path.join(local_app_data, "apm", "Cache")
        # Fallback for missing LOCALAPPDATA
        return os.path.join(os.path.expanduser("~"), "AppData", "Local", "apm", "Cache")

    if sys.platform == "darwin":
        # Honor XDG_CACHE_HOME if explicitly set (power-user override)
        xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
        if xdg:
            return os.path.join(xdg, "apm")
        return os.path.join(os.path.expanduser("~"), "Library", "Caches", "apm")

    # Unix/Linux: follow XDG Base Directory Specification
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        return os.path.join(xdg, "apm")
    return os.path.join(os.path.expanduser("~"), ".cache", "apm")


def _validate_and_ensure(path_str: str) -> Path:
    """Validate and create cache root, returning the Path.

    Raises:
        ValueError: On invalid path (empty, NUL bytes).
    """
    if not path_str:
        raise ValueError("Cache path must not be empty")
    if "\x00" in path_str:
        raise ValueError("Cache path must not contain NUL bytes")

    # Expand ~ and resolve
    expanded = os.path.expanduser(path_str)
    cache_path = Path(expanded).resolve()

    # Ensure it is absolute
    if not cache_path.is_absolute():
        raise ValueError(f"Cache path must be absolute: {path_str}")

    # Create with restrictive permissions
    _ensure_dir(cache_path)
    return cache_path


def _ensure_dir(path: Path) -> None:
    """Create directory with mode 0o700 if it does not exist."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        # Set permissions (best-effort on Windows where modes are no-ops)
        with contextlib.suppress(OSError):
            os.chmod(str(path), 0o700)
    except OSError as exc:
        _log.warning("[!] Failed to create cache directory %s: %s", path, exc)
        raise


def _get_temp_cache_root() -> Path:
    """Return (and lazily create) a temporary cache root.

    The temporary directory is registered for cleanup at process exit
    via atexit.
    """
    global _temp_cache_dir
    if _temp_cache_dir is None:
        import atexit

        _temp_cache_dir = tempfile.mkdtemp(prefix="apm_cache_")
        os.chmod(_temp_cache_dir, 0o700)
        atexit.register(_cleanup_temp_cache)
    return Path(_temp_cache_dir)


def _cleanup_temp_cache() -> None:
    """Remove the temporary cache directory at exit."""
    global _temp_cache_dir
    if _temp_cache_dir is not None:
        import shutil

        shutil.rmtree(_temp_cache_dir, ignore_errors=True)
        _temp_cache_dir = None
