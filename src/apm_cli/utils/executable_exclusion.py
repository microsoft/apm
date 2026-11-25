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


class ProjectShadowedExecutableError(FileNotFoundError):
    """A helper tool exists on PATH, but only inside the enclosing project."""

    def __init__(self, name: str, tool_path: str, root: Path) -> None:
        shown = _entry_spelling(tool_path)
        target = os.path.realpath(shown)
        link = (
            f" (symlink to {target})" if os.path.normcase(target) != os.path.normcase(shown) else ""
        )
        super().__init__(
            f"{name} at {shown}{link} is inside the project {root}; APM only runs "
            f"helper tools from PATH outside the project. Install {name} outside "
            f"{root} or launch apm from outside it."
        )


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
    home = Path.home().resolve()
    ancestors = (cwd, *cwd.parents)
    for has_marker in (lambda d: (d / ".git").exists(), lambda d: manifest_file(d).is_file()):
        for directory in ancestors:
            if directory != home and directory not in home.parents and has_marker(directory):
                return directory
    return None if cwd == home or cwd in home.parents else cwd
