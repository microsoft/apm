"""Runtime binary resolution utilities."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)


def find_runtime_binary(name: str) -> str | None:
    """Return the resolved path to a runtime binary.

    Priority:
      1. ~/.apm/runtimes/<name>  (APM-managed, executable)
      2. shutil.which(name)      (system PATH fallback)

    On Windows the APM-managed binary may carry a ``.exe`` suffix, so both
    ``name`` and ``name.exe`` are checked under ``~/.apm/runtimes/``.

    Raises
    ------
    PathTraversalError
        If *name* contains path-traversal sequences (e.g. ``..``, ``/``,
        ``\\``) or is an absolute path.  This is a security guard against
        user-supplied input that could escape the ``~/.apm/runtimes/``
        directory.
    """
    # Security: reject names containing path-traversal or separator
    # characters before any filesystem path is constructed.
    # Runtime names must be simple identifiers (e.g. "codex", "python").
    if "/" in name or "\\" in name:
        raise PathTraversalError(
            f"Invalid runtime name '{name}': must be a plain binary name "
            "without path separators ('/' or '\\\\')"
        )
    validate_path_segments(name, context="runtime name", reject_empty=True)

    apm_runtimes = Path.home() / ".apm" / "runtimes"

    def _safe_executable(candidate: Path) -> bool:
        """Return True iff *candidate* is an executable file within *apm_runtimes*."""
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            return False
        try:
            ensure_path_within(candidate, apm_runtimes)
        except PathTraversalError:
            return False
        return True

    if sys.platform == "win32":
        apm_path_exe = apm_runtimes / f"{name}.exe"
        if _safe_executable(apm_path_exe):
            return str(apm_path_exe)

    apm_path = apm_runtimes / name
    if _safe_executable(apm_path):
        return str(apm_path)

    return shutil.which(name)
