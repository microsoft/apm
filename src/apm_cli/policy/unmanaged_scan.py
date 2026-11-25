"""Which files the ``unmanaged_files`` policy governs.

The walk over ``unmanaged_files.directories`` (or the default governance
directories): the scan cap, the symlink guard, and the harness-owned local
files skipped by default (#5472). ``policy_checks._check_unmanaged_files``
judges the files this module yields against the lockfile.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

_DEFAULT_GOVERNANCE_DIRS = [
    ".github/agents",
    ".github/instructions",
    ".github/hooks",
    ".cursor/rules",
    ".claude",
    ".opencode",
    ".kiro",
]

# Per-user files a harness writes for itself inside a governance directory.
# Skipped by default at their exact, case-sensitive path; a policy that names
# one in ``unmanaged_files.directories`` governs it again.
HARNESS_LOCAL_FILES: Mapping[str, str] = MappingProxyType(
    {".claude/settings.local.json": "Claude Code's per-user local settings"}
)

MAX_UNMANAGED_SCAN_FILES = 10_000


@dataclass
class GovernedScan:
    """The governed files of one scan, as ``(posix rel path, path)`` pairs."""

    files: list[tuple[str, Path]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    capped: bool = False


def _symlink_escapes_workspace(path: Path, project_root: Path) -> bool:
    """Return True if *path* is a symlink resolving outside *project_root*.

    Guards the traversal so a symlink pointing out of the workspace is never
    followed (no traversal bomb); broken or looping links also count as
    escaping and are skipped.
    """
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        return False
    except (OSError, RuntimeError, ValueError):
        return True


def _lexical_posix(path: str) -> str:
    """Normalize a POSIX *path* lexically: no symlink resolution, no case folding."""
    return posixpath.normpath(path)


def _candidate_files(project_root: Path, dirs: Sequence[str]) -> Iterator[Path]:
    """Yield every non-directory file under each governance entry.

    A directory entry is walked; an entry naming a file yields that file.
    """
    for gov_dir in dirs:
        entry_path = project_root / gov_dir
        if entry_path.is_file():
            if not (
                entry_path.is_symlink() and _symlink_escapes_workspace(entry_path, project_root)
            ):
                yield entry_path
            continue
        if not entry_path.is_dir():
            continue
        # os.walk(followlinks=False) never recurses INTO a directory symlink, so
        # a symlinked dir resolving outside the workspace is never traversed
        # (the house pattern from security/gate.py). File symlinks still appear
        # in the listing and are guarded individually below.
        for dirpath, _subdirs, filenames in os.walk(entry_path, followlinks=False):
            for fname in filenames:
                file_path = Path(dirpath) / fname
                # File-symlink guard: never follow a link out of the workspace.
                if file_path.is_symlink() and _symlink_escapes_workspace(file_path, project_root):
                    continue
                if file_path.is_dir():  # a FIFO or other special file is counted
                    continue
                yield file_path


def scan_governed_files(project_root: Path, directories: Sequence[str] | None) -> GovernedScan:
    """Collect the files the unmanaged-files check judges, once each.

    Stops with ``capped`` set past ``MAX_UNMANAGED_SCAN_FILES`` files. A
    harness local file the policy does not name is skipped with one note.
    """
    named = {_lexical_posix(entry.replace("\\", "/")) for entry in directories or ()}
    scan = GovernedScan()
    seen: set[str] = set()
    for file_path in _candidate_files(project_root, directories or _DEFAULT_GOVERNANCE_DIRS):
        rel = _lexical_posix(file_path.relative_to(project_root).as_posix())
        if rel in seen:
            continue
        seen.add(rel)
        if len(seen) > MAX_UNMANAGED_SCAN_FILES:
            scan.capped = True
            break
        owner = HARNESS_LOCAL_FILES.get(rel)
        if owner is not None and rel not in named:
            scan.notes.append(
                f"Skipped {rel}: {owner} (excluded by default; "
                "name it in unmanaged_files.directories to govern it)"
            )
            continue
        scan.files.append((rel, file_path))
    return scan
