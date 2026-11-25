"""Persistence of compile records: the Claude AGENTS.md record and compiled outputs.

``compilation/claude_agents_md.py`` owns what the Claude record means (#4787);
``CompiledOutputWriter`` records every output path it writes (#5235). This
module only stores them, outside the project (APM home, one file per output
root), so compile never adds files to the repository.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def _record_path(root: Path, folder: str = "compiled-agents-md") -> Path:
    from apm_cli.core.scope import get_apm_home

    key = hashlib.sha256(root.resolve().as_posix().encode("utf-8")).hexdigest()[:32]
    return get_apm_home() / folder / f"{key}.json"


def read_record(root: Path) -> dict:
    """Return the recorded ``files`` map for *root*, or an empty map."""
    try:
        data = json.loads(_record_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


ROOT_CARRIERS = ("AGENTS.md", "CLAUDE.md")


def may_carry(root: Path) -> bool:
    """Whether a compiled ``AGENTS.md`` may stand in for a Claude rule at *root* (#5343).

    The one decision behind ``compiled_agents_md_may_carry`` (the engine fast
    path's decline) and the no-op replay's (#5434): this record exists, or a
    root carrier does. Kept here, light, so a replay asks it without importing
    the compiler: two lstat and one read of the record.
    """
    if read_record(root):
        return True
    return any(os.path.lexists(root / name) for name in ROOT_CARRIERS)


def write_record(root: Path, files: dict) -> None:
    """Replace the record for *root*; a failed write only costs a reminder."""
    from apm_cli.integration.write_seam import atomic_write_text, mkdir

    path = _record_path(root)
    try:
        mkdir(path.parent, parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"version": 1, "files": files}, indent=2) + "\n")
    except OSError:
        return


_OUTPUTS = "compiled-outputs"


def read_compiled_outputs(root: Path) -> list[str]:
    """Return the project-relative paths of every output compile wrote under *root*."""
    try:
        data = json.loads(_record_path(root, _OUTPUTS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    paths = data.get("outputs") if isinstance(data, dict) else None
    return [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []


def record_compiled_outputs(root: Path, written) -> None:
    """Add the outputs compile just *written* under *root*; drop recorded ones now gone.

    A failed write only costs the record; the re-anchor scan still walks
    compile's default placement.
    """
    from apm_cli.integration.write_seam import atomic_write_text, mkdir
    from apm_cli.utils.paths import portable_relpath

    base = Path(root).absolute()
    outputs = {rel for rel in read_compiled_outputs(base) if os.path.lexists(base / rel)}
    for path in written:
        target = Path(path).absolute()
        # An output outside the project keeps its absolute path; the scan reads it as is.
        outputs.add(
            portable_relpath(target, base) if target.is_relative_to(base) else target.as_posix()
        )
    path = _record_path(base, _OUTPUTS)
    try:
        mkdir(path.parent, parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"version": 1, "outputs": sorted(outputs)}) + "\n")
    except OSError:
        return
