"""Cached git binary lookup and subprocess environment sanitization.

Ensures that APM's git subprocess calls use a clean environment free
of ambient git state variables that could bias operations (e.g. when
APM is invoked from within a git repository's hook or worktree).

Preserved variables (user-controlled config for proxy/auth):
- GIT_SSH, GIT_SSH_COMMAND, GIT_ASKPASS, SSH_ASKPASS
- GIT_HTTP_USER_AGENT, GIT_TERMINAL_PROMPT
- GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM

Git state variables stripped after external-process sanitization:
- GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE
- GIT_OBJECT_DIRECTORY, GIT_ALTERNATE_OBJECT_DIRECTORIES
- GIT_COMMON_DIR, GIT_NAMESPACE, GIT_INDEX_VERSION
- GIT_CEILING_DIRECTORIES, GIT_DISCOVERY_ACROSS_FILESYSTEM
- GIT_REPLACE_REF_BASE, GIT_GRAFTS_FILE, GIT_SHALLOW_FILE
"""

from __future__ import annotations

import os
import shutil

from apm_cli.utils.subprocess_env import external_process_env

# Module-level cached git executable path (successful resolutions only).
_git_executable: str | None = None

# Variables that represent ambient git state -- strip these to avoid
# biasing APM's git operations when invoked from within another repo
# or when the calling environment uses git's discovery / replacement
# / grafts overrides.
_STRIP_GIT_VARS: frozenset[str] = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_INDEX_VERSION",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_REPLACE_REF_BASE",
        "GIT_GRAFTS_FILE",
        "GIT_SHALLOW_FILE",
    }
)


def _is_git_auth_config_entry(key: str, value: str) -> bool:
    """Return whether an indexed Git config entry carries an auth header."""
    if "extraheader" in key.casefold():
        return True

    normalized_value = value.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized_value.split("\n"):
        field_name, separator, _field_value = line.lstrip(" \t").partition(":")
        if separator and field_name.rstrip(" \t").casefold() == "authorization":
            return True
    return False


def strip_git_auth_config_entries(env: dict[str, str]) -> int:
    """Remove auth entries from the indexed Git config environment.

    ``GIT_CONFIG_COUNT`` is untrusted process input. Blank, invalid, and
    negative values are treated as zero. Only canonical ``KEY_N`` slots below
    that count are considered, so work is bounded by the actual environment
    size rather than by an arbitrarily large declared count.

    Non-auth entries are preserved in their original index order and compacted
    to contiguous indexes. Every indexed key/value slot, including malformed
    and out-of-range orphans, is removed before the retained set is rebuilt.

    Args:
        env: The subprocess environment mapping to rewrite in place.

    Returns:
        The number of retained entries, which is also the next free index.
    """
    try:
        count = max(0, int(env.get("GIT_CONFIG_COUNT", "0") or "0"))
    except (TypeError, ValueError):
        count = 0

    present_indexes: list[int] = []
    key_prefix = "GIT_CONFIG_KEY_"
    value_prefix = "GIT_CONFIG_VALUE_"
    for name in env:
        if not name.startswith(key_prefix):
            continue
        suffix = name.removeprefix(key_prefix)
        if not suffix or not suffix.isascii() or not suffix.isdigit():
            continue
        if suffix != "0" and suffix.startswith("0"):
            continue
        try:
            index = int(suffix)
        except ValueError:
            continue
        if index < count:
            present_indexes.append(index)

    retained: list[tuple[str, str]] = []
    for index in sorted(present_indexes):
        key = env[f"{key_prefix}{index}"]
        value = env.get(f"{value_prefix}{index}", "")
        if key and not _is_git_auth_config_entry(key, value):
            retained.append((key, value))

    rewritten = {
        key: value
        for key, value in env.items()
        if key != "GIT_CONFIG_COUNT"
        and not key.startswith(key_prefix)
        and not key.startswith(value_prefix)
    }
    if retained:
        rewritten["GIT_CONFIG_COUNT"] = str(len(retained))
        for index, (key, value) in enumerate(retained):
            rewritten[f"{key_prefix}{index}"] = key
            rewritten[f"{value_prefix}{index}"] = value

    env.clear()
    env.update(rewritten)
    return len(retained)


def get_git_executable() -> str:
    """Return the path to the git executable (cached after a successful lookup).

    Uses ``shutil.which("git")`` to locate git on PATH.
    Failed lookups are not cached because PATH can change within a
    long-lived process.

    Returns:
        Absolute or relative path to the git binary.

    Raises:
        FileNotFoundError: If git is not found on PATH.
    """
    global _git_executable
    if _git_executable is not None:
        return _git_executable

    resolved = shutil.which("git")
    if resolved is None:
        raise FileNotFoundError(
            "git executable not found on PATH. Please install git: https://git-scm.com/downloads"
        )
    _git_executable = resolved
    return _git_executable


def git_subprocess_env() -> dict[str, str]:
    """Return a sanitized environment dict for git subprocesses.

    Restores PyInstaller-managed dynamic-library variables first, then
    strips ambient git state variables while preserving user-controlled
    configuration (proxy, auth, SSH settings).

    Returns:
        An external-process-safe copy of ``os.environ`` with problematic
        git variables removed.
    """
    return {k: v for k, v in external_process_env().items() if k not in _STRIP_GIT_VARS}


def reset_git_cache() -> None:
    """Reset the cached git executable (for testing purposes only)."""
    global _git_executable
    _git_executable = None


def git_long_paths_args() -> list[str]:
    """Return ``-c core.longpaths=true`` on Windows, ``[]`` elsewhere.

    Windows enforces a 260-character ``MAX_PATH`` limit by default,
    which the GitCache's deeply-nested ``checkouts_v1/<shard>/<sha>/
    <variant>.incomplete.<pid>.<ns>/`` layout can exceed during
    ``git clone`` -- git fails with ``Filename too long`` while
    creating ``.git/hooks/`` files. Setting ``core.longpaths=true``
    via ``-c`` opts that single subprocess into the long-path API
    without mutating the user's global gitconfig. The flag is a
    no-op on POSIX so callers can prepend it unconditionally.
    """
    if os.name == "nt":
        return ["-c", "core.longpaths=true"]
    return []
