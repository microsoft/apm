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


def _same_directory(first: Path, second: Path) -> bool:
    """Compare by identity, so a case-variant spelling on a case-insensitive FS matches."""
    if first == second:
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def resolved_home() -> Path | None:
    """Return the resolved home directory, or None when it cannot be determined."""
    try:
        return Path.home().resolve()
    except (OSError, RuntimeError):
        return None


def is_home(directory: Path) -> bool:
    """Whether *directory* is the home directory (by identity, not spelling)."""
    home = resolved_home()
    return home is not None and _same_directory(directory, home)


def is_home_or_above(directory: Path) -> bool:
    """Whether *directory* is HOME or one of its ancestors (``/``, ``/home``, ...).

    The single HOME ceiling predicate: a marker there (a dotfiles repo, a stray
    manifest) is not a project.
    """
    home = resolved_home()
    return home is not None and any(_same_directory(directory, d) for d in (home, *home.parents))


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
