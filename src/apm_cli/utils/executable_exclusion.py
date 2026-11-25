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
