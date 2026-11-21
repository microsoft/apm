"""A ``codex`` executable that only records that it ran (#3920).

APM never locates or runs the Codex CLI. Tests put this spy first on PATH
and assert its marker file is absent after driving APM.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def write_codex_spy(bin_dir: Path, marker: Path) -> Path:
    """Write the spy into *bin_dir* and return *bin_dir* for PATH."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n"
    (bin_dir / "codex-spy.py").write_text(script, encoding="utf-8")
    if os.name == "nt":
        (bin_dir / "codex.cmd").write_text(
            f'@"{sys.executable}" "{bin_dir / "codex-spy.py"}"\r\n', encoding="utf-8"
        )
    else:
        launcher = bin_dir / "codex"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "codex-spy.py"}"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)
    return bin_dir
