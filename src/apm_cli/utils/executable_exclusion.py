"""Project root policy for helper-executable lookup (see ``git_env``)."""

from __future__ import annotations

import os
from pathlib import Path

_last_shadow: dict[str, str] = {}


class ProjectShadowedExecutableError(FileNotFoundError):
    """A helper tool exists on PATH, but only inside the enclosing project."""

    def __init__(self, name: str, tool_path: str, root: Path) -> None:
        tool_path = os.path.realpath(tool_path)  # stored spelling, not the PATHEXT one
        super().__init__(
            f"{name} at {tool_path} is inside the project {root}; APM only runs "
            f"helper tools from PATH outside the project. Install {name} outside "
            f"{root} or launch apm from outside it."
        )


def note_shadow(name: str, error: FileNotFoundError) -> None:
    """Remember why the last lookup of *name* missed; return None for the caller."""
    if isinstance(error, ProjectShadowedExecutableError):
        _last_shadow[name] = str(error)
    else:
        _last_shadow.pop(name, None)


def forget_shadow(name: str, path: str) -> str:
    """Drop a stale shadow note once *name* resolves to *path*, and return it."""
    _last_shadow.pop(name, None)
    return path


def not_found_message(name: str, default: str) -> str:
    """Return the shadowed-in-project message for *name*, else *default*."""
    return _last_shadow.get(name, default)


def executable_exclusion_root() -> Path | None:
    """Return the project root containing the cwd, or None to exclude nothing.

    The nearest ``.git`` root wins, else the nearest APM manifest root, else
    the cwd. A marker at HOME or above (a dotfiles repo, a stray manifest,
    ``/``) is not a project and is skipped; when the cwd itself is HOME or
    above nothing is excluded.
    """
    from apm_cli.core.scope import manifest_file

    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    ancestors = (cwd, *cwd.parents)
    for has_marker in (lambda d: (d / ".git").exists(), lambda d: manifest_file(d).is_file()):
        for directory in ancestors:
            if directory != home and directory not in home.parents and has_marker(directory):
                return directory
    return None if cwd == home or cwd in home.parents else cwd
