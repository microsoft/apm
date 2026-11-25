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


def is_home_or_above(directory: Path, home: Path) -> bool:
    """Whether resolved *directory* is resolved *home* or one of its ancestors, by identity.

    The one HOME bound (#5470): such a directory is never a project, so a
    ``.git`` or manifest there (a dotfiles repo, a stray marker) is skipped,
    and a #5418 lockfile identity never climbs to it. A spelling that differs
    only in case or Unicode normalization is the same directory where the
    volume folds: ``samefile`` decides, so a case-sensitive volume never
    merges two directories.
    """
    if directory == home or directory in home.parents:
        return True
    from apm_cli.utils.paths import spelling_key

    key = spelling_key(str(directory))
    for candidate in (home, *home.parents):
        if spelling_key(str(candidate)) != key:
            continue
        try:
            if os.path.samefile(directory, candidate):
                return True
        except OSError:
            continue
    return False


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
            if not is_home_or_above(directory, home) and has_marker(directory):
                return directory
    return None if is_home_or_above(cwd, home) else cwd
