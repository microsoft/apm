"""Project root policy for helper-executable lookup (see ``git_env``)."""

from __future__ import annotations

import functools
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


def _same_directory(first: Path, second: Path) -> bool:
    """Compare by identity, so a case-variant spelling on a case-insensitive FS matches."""
    if first == second:
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


@functools.lru_cache(maxsize=4)
def _resolve(path: Path) -> Path | None:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def resolved_home() -> Path | None:
    """Return the resolved home directory, or None when it cannot be determined."""
    try:
        return _resolve(Path.home())
    except RuntimeError:
        return None


def is_home(directory: Path) -> bool:
    """Whether *directory* is the home directory (by identity, not spelling)."""
    home = resolved_home()
    return home is not None and _same_directory(directory, home)


@functools.lru_cache(maxsize=4)
def _home_chain_identities(home: Path) -> frozenset[tuple[int, int]]:
    """(dev, ino) of *home* and each ancestor, stat'ed once per resolved home."""
    identities = set()
    for level in (home, *home.parents):
        try:
            info = os.stat(level)
        except OSError:
            continue
        identities.add((info.st_dev, info.st_ino))
    return frozenset(identities)


def is_home_or_above(directory: Path) -> bool:
    """Whether *directory* is HOME or one of its ancestors (``/``, ``/home``, ...).

    The single HOME ceiling predicate: a marker there (a dotfiles repo, a stray
    manifest) is not a project. Identity-based, so a case-variant or symlinked
    spelling matches; HOME's chain is stat'ed once, then one stat per query.
    """
    home = resolved_home()
    if home is None:
        return False
    if directory == home or directory in home.parents:
        return True
    try:
        wanted = os.stat(directory)
    except OSError:
        return False
    return (wanted.st_dev, wanted.st_ino) in _home_chain_identities(home)


def executable_exclusion_root() -> Path | None:
    """Return the project root containing the cwd, or None to exclude nothing.

    The nearest ``.git`` root wins, else the nearest APM manifest root, else
    the cwd. A marker at HOME or above (a dotfiles repo, a stray manifest,
    ``/``) is not a project and is skipped; when the cwd itself is HOME or
    above nothing is excluded.
    """
    from apm_cli.core.scope import manifest_file

    cwd = Path.cwd().resolve()
    ancestors = (cwd, *cwd.parents)
    for has_marker in (lambda d: (d / ".git").exists(), lambda d: manifest_file(d).is_file()):
        for directory in ancestors:
            if not is_home_or_above(directory) and has_marker(directory):
                return directory
    return None if is_home_or_above(cwd) else cwd
