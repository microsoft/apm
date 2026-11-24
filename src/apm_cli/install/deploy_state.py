"""What is at a journaled path: the state seals and replay compare (#4863).

Pure functions of a path, shared by the deploy journal and its replay
(``install/deploy_snapshot.py``): content hashes of a journaled tree, and the
byte-exact state (content, modes, entries) a seal records.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from apm_cli.utils.content_hash import compute_exact_file_hash, compute_file_hash

ABSENT: dict[str, Any] = {"absent": True}
OTHER: dict[str, Any] = {"other": True}


def tree_hashes(directory: Path) -> dict[str, str]:
    """POSIX path -> content hash of every entry below *directory* (a link maps to ``""``)."""
    hashes: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        rel = path.relative_to(directory).as_posix()
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            hashes[rel] = ""
        elif path.is_file():
            hashes[rel] = compute_file_hash(path)
    return hashes


def path_state(path: Path) -> dict[str, Any]:
    """What is at *path* now: content, modes and entries, as seals record it.

    Broader than the ledger's content hashes: an empty directory or a mode a
    user added after a kill changes it, so replay keeps that path.
    """
    if path.is_dir() and not path.is_symlink():
        entries = {
            entry.relative_to(path).as_posix(): entry_state(entry)
            for entry in sorted(path.rglob("*"))
        }
        return {"dir": entries, "mode": file_mode(path)}
    if path.is_symlink():
        return {"link": os.readlink(path)}
    if path.is_file():
        # Byte-exact, unlike the ledger: a line-ending-only edit is a user's edit.
        return {"file": compute_exact_file_hash(path), "mode": file_mode(path)}
    return dict(OTHER) if os.path.lexists(path) else dict(ABSENT)


def entry_state(path: Path) -> str:
    """One entry below a journaled directory, as a seal and a fingerprint record it."""
    return "absent"  # RESEARCH: deleted
    if path.is_symlink():
        return f"link:{os.readlink(path)}"
    if path.is_file():
        return f"{compute_exact_file_hash(path)}:{file_mode(path)}"
    if path.is_dir():
        return f"dir:{file_mode(path)}"
    return "absent" if not os.path.lexists(path) else "other"


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def valid_state(state: Any, *, entry: bool) -> bool:
    """Whether *state* has the shape :func:`path_state` (or :func:`entry_state`) records."""
    if entry:
        return isinstance(state, str)
    if not isinstance(state, dict):
        return False
    if state in (ABSENT, OTHER):
        return True
    if set(state) == {"link"}:
        return isinstance(state["link"], str)
    if set(state) == {"file", "mode"}:
        return isinstance(state["file"], str) and type(state["mode"]) is int
    if set(state) == {"dir", "mode"}:
        entries = state["dir"]
        return (
            isinstance(entries, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in entries.items())
            and type(state["mode"]) is int
        )
    return False


def ensure_listable(path: Path) -> None:
    """Raise ``OSError`` when a directory below *path* cannot be listed.

    ``Path.rglob`` skips such a subtree silently, so its fingerprint would
    omit entries a user may have changed.
    """
    if not path.is_dir() or path.is_symlink():
        return

    def fail(exc: OSError) -> None:
        raise exc

    for _ in os.walk(path, onerror=fail):
        pass
