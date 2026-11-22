"""Persistence of the Claude AGENTS.md compile record (#4787).

``compilation/claude_agents_md.py`` owns what the record means; this module
only stores it, outside the project (APM home, one file per output root), so
compile never adds files to the repository and compiled outputs keep going
through ``CompiledOutputWriter``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _record_path(root: Path) -> Path:
    from apm_cli.core.scope import get_apm_home

    key = hashlib.sha256(root.resolve().as_posix().encode("utf-8")).hexdigest()[:32]
    return get_apm_home() / "compiled-agents-md" / f"{key}.json"


def read_record(root: Path) -> dict:
    """Return the recorded ``files`` map for *root*, or an empty map."""
    try:
        data = json.loads(_record_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def write_record(root: Path, files: dict) -> None:
    """Replace the record for *root*; a failed write only costs a reminder."""
    from apm_cli.integration.write_seam import atomic_write_text, mkdir

    path = _record_path(root)
    try:
        mkdir(path.parent, parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"version": 1, "files": files}, indent=2) + "\n")
    except OSError:
        return
