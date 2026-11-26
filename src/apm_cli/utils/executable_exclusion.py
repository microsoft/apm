"""Project root policy for helper-executable lookup (see ``git_env``)."""

from __future__ import annotations

import os
from pathlib import Path

_last_shadow: dict[str, str] = {}


def _entry_spelling(tool_path: str) -> str:
    """Return the PATH entry as stored on disk, without following the entry itself."""
    path = Path(tool_path)
    parent = os.path.realpath(path.parent)
    try:
        names = os.listdir(parent)
    except OSError:
        names = []
    wanted = path.name
    stored = (
        wanted
        if wanted in names
        else next((n for n in names if n.lower() == wanted.lower()), wanted)
    )
    return os.path.join(parent, stored)


def _within(path: str, root: Path) -> bool:
    return Path(os.path.normcase(path)).is_relative_to(os.path.normcase(root))


class ProjectShadowedExecutableError(FileNotFoundError):
    """A helper tool exists on PATH, but only inside the enclosing project."""

    def __init__(self, name: str, tool_path: str, root: Path) -> None:
        shown = _entry_spelling(tool_path)
        target = os.path.realpath(shown)
        fix = f"Install {name} outside {root} or launch apm from outside it."
        if os.path.normcase(target) == os.path.normcase(shown):
            where = f"{name} at {shown} is inside the project {root}"
        elif _within(shown, root):
            where = f"{name} at {shown} (symlink to {target}) is inside the project {root}"
        else:
            where = f"{name} at {shown} links to {target}, which is inside the project {root}"
            fix = (
                f"Install {name} outside {root} (not a link into it) or launch apm from outside it."
            )
        super().__init__(
            f"{where}; APM only runs helper tools from PATH outside the project. {fix}"
        )


def same_directory(first: Path, second: Path) -> bool:
    """Compare by identity, so a case-variant or symlinked spelling matches."""
    if first == second:
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


_RESOLVED_HOME: dict[str, Path] = {}
_HOME_CHAIN: dict[Path, frozenset[tuple[int, int]]] = {}
_LEVEL_VERDICTS: dict[tuple[Path, Path], bool] = {}


def resolved_home() -> Path | None:
    """Return the resolved home directory, or None when it cannot be determined.

    Only a successful resolution is remembered: a transient failure must not
    disable the HOME ceiling for the rest of the process.
    """
    try:
        raw = str(Path.home())
        cached = _RESOLVED_HOME.get(raw)
        if cached is None:
            cached = _RESOLVED_HOME[raw] = Path(raw).resolve()
        return cached
    except (OSError, RuntimeError):
        return None


def is_home(directory: Path) -> bool:
    """Whether *directory* is the home directory (by identity, not spelling)."""
    home = resolved_home()
    return home is not None and same_directory(directory, home)


def _home_chain_identities(home: Path) -> frozenset[tuple[int, int]] | None:
    """(dev, ino) of *home* and each ancestor; cached only when every level stat'ed."""
    cached = _HOME_CHAIN.get(home)
    if cached is not None:
        return cached
    try:
        identities = frozenset(
            (info.st_dev, info.st_ino) for info in map(os.stat, (home, *home.parents))
        )
    except OSError:
        return None
    _HOME_CHAIN[home] = identities
    return identities


def is_home_or_above(directory: Path, home: Path | None) -> bool:
    """Whether *directory* is *home* or one of its ancestors (``/``, ``/home``, ...).

    The single HOME ceiling predicate: a marker there (a dotfiles repo, a stray
    manifest) is not a project, and a #5418 lockfile identity never climbs to
    it (#5470). Identity-based, so a case-variant or symlinked
    spelling matches; HOME's chain and each directory verdict are stat'ed once per process.
    A missing *home* (``resolved_home()`` returned None) disables the check.
    """
    if home is None:
        return False
    if directory == home or directory in home.parents:
        return True
    known = _LEVEL_VERDICTS.get((home, directory))
    if known is not None:
        return known
    chain = _home_chain_identities(home)
    try:
        wanted = os.stat(directory)
    except OSError:
        # Fail closed: an unreadable directory spelled as a HOME level stays a ceiling.
        levels = {os.path.normcase(str(level)) for level in (home, *home.parents)}
        return os.path.normcase(str(directory)) in levels
    if chain is None:
        return any(same_directory(directory, level) for level in (home, *home.parents))
    verdict = _LEVEL_VERDICTS[(home, directory)] = (wanted.st_dev, wanted.st_ino) in chain
    return verdict


def note_shadow(name: str, error: FileNotFoundError) -> None:
    """Remember why the last lookup of *name* missed; return None for the caller."""
    if isinstance(error, ProjectShadowedExecutableError):
        _last_shadow[name] = str(error)
    else:
        _last_shadow.pop(name, None)


def forget_shadow(name: str) -> None:
    """Drop a stale shadow note before *name* is looked up again."""
    _last_shadow.pop(name, None)


def not_found_message(default: str, *names: str) -> str:
    """Return the shadowed-in-project message for the first of *names* that has one."""
    return next((_last_shadow[n] for n in names if n in _last_shadow), default)


def executable_exclusion_root() -> Path | None:
    """Return the project root containing the cwd, or None to exclude nothing.

    The nearest ``.git`` root wins, else the nearest APM manifest root, else
    the cwd. A marker at HOME or above (a dotfiles repo, a stray manifest,
    ``/``) is not a project and is skipped; when the cwd itself is HOME or
    above nothing is excluded.
    """
    from apm_cli.core.scope import manifest_file

    cwd = Path.cwd().resolve()
    home = resolved_home()
    ancestors = (cwd, *cwd.parents)
    for has_marker in (lambda d: (d / ".git").exists(), lambda d: manifest_file(d).is_file()):
        for directory in ancestors:
            if not is_home_or_above(directory, home) and has_marker(directory):
                return directory
    return None if is_home_or_above(cwd, home) else cwd
