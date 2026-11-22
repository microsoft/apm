"""Deferred GitPython access for the dependency transport modules.

Importing ``git`` (GitPython) loads about 60 modules and runs ``git version``
in a subprocess. Install paths that never clone (a no-op install, ``--help``)
must not pay that, so transport modules import GitPython only where a Git
object is actually built.
"""

from __future__ import annotations

import sys
from typing import Any


class _GitPythonNotLoaded(Exception):
    """Placeholder that no code raises; matches nothing in ``except`` clauses."""


def git_command_error() -> type[Exception]:
    """Return ``GitCommandError`` for ``except``/``isinstance`` checks without importing GitPython.

    A ``GitCommandError`` instance can only exist after GitPython was imported,
    so while ``git.exc`` is not loaded a never-raised placeholder is equivalent.
    """
    # Another thread may still be executing ``git.exc``; until the class exists
    # no instance of it can have been raised either.
    error = getattr(sys.modules.get("git.exc"), "GitCommandError", None)
    return _GitPythonNotLoaded if error is None else error


def new_git_command_error(*args: object, **kwargs: object) -> Exception:
    """Build a real ``GitCommandError`` (imports GitPython on this error path only)."""
    from git.exc import GitCommandError

    return GitCommandError(*args, **kwargs)


def loaded_git_module(namespace: dict[str, Any]) -> Any:
    """Return the ``git`` module bound in *namespace* or fully imported, else None.

    Test doubles patched onto a module's ``git`` attribute win. A ``git``
    module another thread is still importing counts as not loaded. This never
    imports GitPython itself.
    """
    bound = namespace.get("git")
    if bound is not None:
        return bound
    module = sys.modules.get("git")
    if module is None or getattr(getattr(module, "__spec__", None), "_initializing", False):
        return None
    return module


def bind_git_name(namespace: dict[str, Any], name: str) -> Any:
    """Return *namespace*'s ``git``/``Repo``/... binding, importing GitPython on first use.

    Keeps the module attribute a stable patch target: a value already bound in
    *namespace* (including a test double) is returned unchanged.
    """
    if name in namespace:
        return namespace[name]
    import git

    value = git if name == "git" else getattr(git, name)
    namespace[name] = value
    return value
