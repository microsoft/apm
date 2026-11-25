"""Project root policy for helper-executable lookup (see ``git_env``)."""

from __future__ import annotations

from pathlib import Path


class ProjectShadowedExecutableError(FileNotFoundError):
    """A helper tool exists on PATH, but only inside the enclosing project."""

    def __init__(self, name: str, tool_path: str, root: Path) -> None:
        super().__init__(
            f"{name} at {tool_path} is inside the project {root}; APM only runs "
            f"helper tools from PATH outside the project. Install {name} outside "
            f"{root} or launch apm from outside it."
        )


def executable_exclusion_root() -> Path | None:
    """Return the nearest project root containing the cwd, or None to exclude nothing.

    The nearest directory holding ``.git`` or the APM manifest wins; with no
    marker the cwd is the root. A marker at HOME or above (a dotfiles repo, a
    stray manifest, ``/``) is not a project: the cwd is the root instead, and
    when the cwd is HOME or above nothing is excluded.
    """
    from apm_cli.core.scope import manifest_file

    cwd = Path.cwd().resolve()
    root = cwd
    for directory in (cwd, *cwd.parents):
        if (directory / ".git").exists() or manifest_file(directory).is_file():
            root = directory
            break
    home = Path.home().resolve()
    if root == home or root in home.parents:
        root = cwd  # a dotfiles repo at HOME is not a project: keep the cwd guard
    return None if root == home or root in home.parents else root
