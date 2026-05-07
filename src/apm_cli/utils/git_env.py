"""Cached git binary lookup and subprocess environment sanitization.

Ensures that APM's git subprocess calls use a clean environment free
of ambient git state variables that could bias operations (e.g. when
APM is invoked from within a git repository's hook or worktree).

Preserved variables (user-controlled config for proxy/auth):
- GIT_SSH, GIT_SSH_COMMAND, GIT_ASKPASS, SSH_ASKPASS
- GIT_HTTP_USER_AGENT, GIT_TERMINAL_PROMPT
- GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM

Stripped variables (ambient git state):
- GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE
- GIT_OBJECT_DIRECTORY, GIT_ALTERNATE_OBJECT_DIRECTORIES
- GIT_COMMON_DIR, GIT_NAMESPACE, GIT_INDEX_VERSION
- GIT_CEILING_DIRECTORIES, GIT_DISCOVERY_ACROSS_FILESYSTEM
- GIT_REPLACE_REF_BASE, GIT_GRAFTS_FILE, GIT_SHALLOW_FILE
"""

from __future__ import annotations

import os
import shutil

# Module-level cached git executable path (resolved once per process)
_git_executable: str | None = None
_git_resolved: bool = False

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


def get_git_executable() -> str:
    """Return the path to the git executable (cached after first lookup).

    Uses ``shutil.which("git")`` to locate git on PATH.

    Returns:
        Absolute or relative path to the git binary.

    Raises:
        FileNotFoundError: If git is not found on PATH.
    """
    global _git_executable, _git_resolved
    if _git_resolved:
        if _git_executable is None:
            raise FileNotFoundError(
                "git executable not found on PATH. "
                "Please install git: https://git-scm.com/downloads"
            )
        return _git_executable

    _git_executable = shutil.which("git")
    _git_resolved = True
    if _git_executable is None:
        raise FileNotFoundError(
            "git executable not found on PATH. Please install git: https://git-scm.com/downloads"
        )
    return _git_executable


def git_subprocess_env() -> dict[str, str]:
    """Return a sanitized environment dict for git subprocesses.

    Strips ambient git state variables while preserving user-controlled
    configuration (proxy, auth, SSH settings).

    Returns:
        A copy of ``os.environ`` with problematic git variables removed.
    """
    return {k: v for k, v in os.environ.items() if k not in _STRIP_GIT_VARS}


def reset_git_cache() -> None:
    """Reset the cached git executable (for testing purposes only)."""
    global _git_executable, _git_resolved
    _git_executable = None
    _git_resolved = False
