"""Where the Codex native-plugin ownership journal lives (no registrar imports)."""

from __future__ import annotations

from pathlib import Path


def ledger_path_for(modules_dir: Path) -> Path:
    """Keep ownership outside the disposable package materialization directory."""
    return modules_dir.parent / ".codex-native" / "ownership.json"
