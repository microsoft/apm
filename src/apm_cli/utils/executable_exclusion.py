"""Project root policy for helper-executable lookup (see ``git_env``)."""

from __future__ import annotations

import os
from pathlib import Path


class ProjectShadowedExecutableError(FileNotFoundError):
    """A helper tool exists on PATH, but only inside the enclosing project."""

    def __init__(self, name: str, tool_path: str, root: Path) -> None:
        super().__init__(
            f"{name} at {tool_path} is inside the project {root}; APM only runs "
            f"helper tools from PATH outside the project. Install {name} outside "
            f"{root} or launch apm from outside it."
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
    """(dev, ino) of *home*; cached only when it stat'ed. Ancestors match by spelling."""
    cached = _HOME_CHAIN.get(home)
    if cached is not None:
        return cached
    try:
        info = os.stat(home)
    except OSError:
        return None
    identities = _HOME_CHAIN[home] = frozenset({(info.st_dev, info.st_ino)})
    return identities


def is_home_or_above(directory: Path, home: Path | None) -> bool:
    """Whether *directory* is *home* or one of its ancestors (``/``, ``/home``, ...).

    The single HOME ceiling predicate: a marker there (a dotfiles repo, a stray
    manifest) is not a project. *home* is resolved, so a case-variant spelling of
    any level matches by case-folded path and HOME itself also by (dev, ino);
    each verdict is stat'ed once per process. A missing *home*
    (``resolved_home()`` returned None) disables the check.
    """
    if home is None:
        return False
    if directory == home or directory in home.parents:
        return True
    levels = {os.path.normcase(str(level)).casefold() for level in (home, *home.parents)}
    if os.path.normcase(str(directory)).casefold() in levels:
        return True
    known = _LEVEL_VERDICTS.get((home, directory))
    if known is not None:
        return known
    chain = _home_chain_identities(home)
    try:
        wanted = os.stat(directory)
    except OSError:
        return False
    if chain is None:
        return same_directory(directory, home)
    verdict = _LEVEL_VERDICTS[(home, directory)] = (wanted.st_dev, wanted.st_ino) in chain
    return verdict


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
