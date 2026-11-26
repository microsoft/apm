"""Where APM records the Codex native registrations it owns.

Kept free of the registrar's imports, so a caller that only asks whether the
ledger exists (the install fast path, #5491) never loads the registrar.
"""

from __future__ import annotations

from pathlib import Path


def ledger_path_for(modules_dir: Path) -> Path:
    """Keep ownership outside the disposable package materialization directory."""
    return modules_dir.parent / ".codex-native" / "ownership.json"
