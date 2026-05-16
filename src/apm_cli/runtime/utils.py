"""Runtime binary resolution utilities."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def find_runtime_binary(name: str) -> str | None:
    """Return the resolved path to a runtime binary.

    Priority:
      1. ~/.apm/runtimes/<name>  (APM-managed, executable)
      2. shutil.which(name)      (system PATH fallback)

    On Windows the APM-managed binary may carry a ``.exe`` suffix, so both
    ``name`` and ``name.exe`` are checked under ``~/.apm/runtimes/``.
    """
    apm_runtimes = Path.home() / ".apm" / "runtimes"

    if sys.platform == "win32":
        apm_path_exe = apm_runtimes / f"{name}.exe"
        if apm_path_exe.is_file() and os.access(apm_path_exe, os.X_OK):
            return str(apm_path_exe)

    apm_path = apm_runtimes / name
    if apm_path.is_file() and os.access(apm_path, os.X_OK):
        return str(apm_path)

    return shutil.which(name)
