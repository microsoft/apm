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
    refused: list[str] = field(default_factory=list)
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


def _walked_label(project_root: Path, file_path: Path) -> str:
    """Return the workspace-relative label of a walked file.

    The lexical form is used only when it names the same file: a ``..`` after
    a symlinked directory leads elsewhere, so the as-walked form is kept and
    can never pose as an exempt path (fail closed). Only a path holding ``.``
    or ``..`` pays for the realpath comparison.
    """
    walked = file_path.relative_to(project_root).as_posix()
    lexical = _lexical_posix(walked)
    if lexical == walked:
        return walked
    lexical_parent = project_root / posixpath.dirname(lexical)
    if os.path.realpath(file_path.parent) == os.path.realpath(lexical_parent):
        return lexical
    return walked


class EntryOutsideProject(ValueError):
    """An ``unmanaged_files.directories`` entry that names a path outside the project."""


def _relpath(target: Path | str, base: Path | str) -> str:
    """``os.path.relpath`` that names another Windows drive as outside (``..``)."""
    try:
        return os.path.relpath(target, base)
    except ValueError:
        return os.pardir


def _outside(rel: str) -> bool:
    return rel == os.pardir or rel.startswith(os.pardir + os.sep)


def _workspace_entry(project_root: Path, entry: str) -> str:
    """*entry* as a project-relative POSIX path; refuse one outside the project.

    A relative entry is kept as written (the walk labels what it finds). An
    absolute entry inside the project becomes project-relative. The resolved
    path is authoritative: an entry that resolves outside the project, through
    ``..``, an absolute path or a link, is refused. Only when links cannot be
    resolved does the lexical path decide.
    """
    raw = entry.replace("\\", "/")
    target = Path(raw) if os.path.isabs(raw) else project_root / raw
    try:
        real = _relpath(os.path.realpath(target), os.path.realpath(project_root))
    except OSError:
        real = _relpath(os.path.normpath(target), os.path.normpath(project_root))
    if _outside(real):
        raise EntryOutsideProject(
            f"unmanaged_files.directories entry '{entry}' is outside the project; "
            "use a path relative to the project root"
        )
    if not os.path.isabs(raw):
        return raw
    lexical = _relpath(os.path.normpath(target), os.path.normpath(project_root))
    return Path(real if _outside(lexical) else lexical).as_posix()


def _real_label(project_root: Path, file_path: Path) -> str:
    """The project-relative path of *file_path* with its parent's links resolved."""
    parent = os.path.relpath(os.path.realpath(file_path.parent), os.path.realpath(project_root))
    return posixpath.join(Path(parent).as_posix(), file_path.name).removeprefix("./")


def _candidate_files(project_root: Path, dirs: Sequence[str]) -> Iterator[Path]:
    """Yield every non-directory file under each governance entry.

    A directory entry is walked; an entry naming any other existing path (a
    file, a FIFO or other special file, a link) yields that path.
    """
    for gov_dir in dirs:
        entry_path = project_root / gov_dir
        # A default governance dir linked out of the project is never walked.
        if _symlink_escapes_workspace(entry_path, project_root):
            continue
        if os.path.lexists(entry_path) and not entry_path.is_dir():
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
    An entry outside the project is refused (``refused``) and never walked;
    the other entries are still scanned.
    """
    scan = GovernedScan()
    entries: list[str] = []
    for entry in directories or ():
        try:
            entries.append(_workspace_entry(project_root, entry))
        except EntryOutsideProject as exc:
            scan.refused.append(str(exc))
    named = {_walked_label(project_root, project_root / entry) for entry in entries}
    seen: set[str] = set()
    scanned = 0
    for file_path in _candidate_files(
        project_root, entries if directories else _DEFAULT_GOVERNANCE_DIRS
    ):
        rel = _walked_label(project_root, file_path)
        # A label the walk kept unnormalized also claims its real path, so one
        # file reached under two labels is judged once.
        real = _real_label(project_root, file_path) if rel != _lexical_posix(rel) else rel
        # normcase: one key per entry on a case-insensitive (Windows) volume.
        keys = {os.path.normcase(rel), os.path.normcase(real)}
        if not keys.isdisjoint(seen):
            continue
        seen.update(keys)
        scanned += 1
        if scanned > MAX_UNMANAGED_SCAN_FILES:
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
