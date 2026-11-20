"""Single owner of when ``apm.lock.yaml`` reaches disk (#4086).

``LockFile.write`` decides *what* a lockfile holds (generator stamp, legacy
timestamp, no-op detection); this module decides *when* those bytes are
persisted, and by whom.

The writers form a closed set: each :class:`LockfileWriter` names one command
entry point, which opens a write scope with :func:`lockfile_writer`.  Inside a
scope every ``LockFile.write`` is staged in memory instead of persisted, every
``LockFile.read`` (and :func:`lockfile_exists`) sees the staged document, and
the outermost scope commits the final document once when it exits.  A command
therefore writes the lockfile at most once, however many phases record state
in it.  Nested scopes (``apm update`` running the install service) join the
outer one.

Two explicit commit points flush earlier: :func:`flush_lockfile_writes`
before state outside APM's transaction must agree with the lockfile (native
plugin registration, user lifecycle scripts).  Removal and rollback go
through :func:`remove_lockfile` and :func:`restore_lockfile_bytes` so a staged
document never resurrects a lockfile a writer deleted or restored.

Outside a scope (library callers, unit tests) ``LockFile.write`` persists
immediately, exactly as before.  ``--frozen`` suppression is decided by
``LockFile.write`` before it reaches this module, so withheld writes are
never staged.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import ParamSpec, TypeVar

_log = logging.getLogger(__name__)

_P = ParamSpec("_P")
_R = TypeVar("_R")


class LockfileWriter(StrEnum):
    """The closed set of command entry points allowed to persist the lockfile.

    ``apm mcp install`` forwards to ``apm install --mcp`` (``INSTALL_MCP``);
    ``apm deps update`` is an ``UPDATE``; ``apm lock`` runs the install
    service in lockfile-only mode under ``LOCK``.
    """

    INSTALL = "install"
    INSTALL_MCP = "install-mcp"
    INSTALL_BUNDLE = "install-bundle"
    LOCK = "lock"
    UPDATE = "update"
    UNINSTALL = "uninstall"
    PRUNE = "prune"
    COMPILE = "compile"


class _Removed:
    """Marks a lockfile a writer deleted inside the scope."""


_REMOVED = _Removed()


@dataclass
class _WriteScope:
    writer: LockfileWriter
    pending: dict[Path, str | _Removed] = field(default_factory=dict)


_active_scope: ContextVar[_WriteScope | None] = ContextVar("apm_lockfile_write_scope", default=None)


def _key(path: Path) -> Path:
    # Absolute, not resolved: a symlinked lockfile must stay a symlink so the
    # writer can refuse it, and a cwd change later in the command must not
    # move the destination.
    return Path(os.path.abspath(path))


def active_writer() -> LockfileWriter | None:
    """Return the writer whose scope is active, or ``None`` outside a scope."""
    scope = _active_scope.get()
    return None if scope is None else scope.writer


@contextmanager
def lockfile_write_scope(writer: LockfileWriter) -> Iterator[None]:
    """Stage lockfile writes for *writer* and commit them once on exit.

    The commit runs on every exit path: a failing command keeps the lockfile
    records of what it already deployed, as it did when each phase wrote
    immediately.
    """
    if not isinstance(writer, LockfileWriter):
        raise TypeError(f"{writer!r} is not a registered lockfile writer")
    if _active_scope.get() is not None:
        yield
        return
    scope = _WriteScope(writer)
    token = _active_scope.set(scope)
    try:
        yield
    except BaseException:
        try:
            _commit(scope)
        except Exception as exc:
            from ..utils.console import _rich_warning

            _rich_warning(f"apm.lock.yaml could not be updated: {exc}", symbol="warning")
        raise
    else:
        _commit(scope)
    finally:
        _active_scope.reset(token)


def lockfile_writer(writer: LockfileWriter) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Decorate a command entry point as the lockfile writer *writer*."""

    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(function)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with lockfile_write_scope(writer):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def stage_lockfile(path: Path, serialized: str) -> bool:
    """Stage *serialized* for *path* when a scope is active.

    Returns ``False`` outside a scope, telling ``LockFile.write`` to persist
    immediately.  Refusals (symlinked or newer-generator lockfiles) were
    already raised by ``LockFile.write`` at the call site.
    """
    scope = _active_scope.get()
    if scope is None:
        return False
    key = _key(path)
    # Re-insert so commit order follows the latest write.
    scope.pending.pop(key, None)
    scope.pending[key] = serialized
    return True


def _disk_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def staged_lockfile_text(path: Path) -> str | None:
    """Return the document a scope staged for *path*, else its on-disk text.

    ``None`` means no lockfile: never written, or removed in this scope.
    """
    scope = _active_scope.get()
    if scope is not None:
        staged = scope.pending.get(_key(path))
        if isinstance(staged, _Removed):
            return None
        if staged is not None:
            return staged
    return _disk_text(path)


def has_staged_lockfile(path: Path) -> bool | None:
    """``True``/``False`` when a scope staged or removed *path*; ``None`` otherwise."""
    scope = _active_scope.get()
    if scope is None:
        return None
    staged = scope.pending.get(_key(path))
    if staged is None:
        return None
    return not isinstance(staged, _Removed)


def lockfile_exists(path: Path) -> bool:
    """Whether *path* holds a lockfile, counting writes staged in this scope."""
    staged = has_staged_lockfile(path)
    return path.exists() if staged is None else staged


def remove_lockfile(path: Path) -> None:
    """Delete the lockfile at *path* and anything staged for it."""
    scope = _active_scope.get()
    if scope is not None:
        key = _key(path)
        scope.pending.pop(key, None)
        scope.pending[key] = _REMOVED
    path.unlink(missing_ok=True)


def restore_lockfile_bytes(path: Path, data: bytes) -> None:
    """Roll *path* back to *data*, discarding anything staged for it."""
    from ..utils.atomic_io import atomic_write_text

    scope = _active_scope.get()
    if scope is not None:
        scope.pending.pop(_key(path), None)
    atomic_write_text(path, data.decode("utf-8"), normalize_line_endings=False)


def flush_lockfile_writes() -> None:
    """Commit staged lockfile writes now; the scope stays open."""
    scope = _active_scope.get()
    if scope is not None:
        _commit(scope)


def _commit(scope: _WriteScope) -> None:
    from ..utils.atomic_io import atomic_write_text, umask_file_mode
    from .lockfile import _same_document, ensure_lockfile_writable

    while scope.pending:
        key = next(iter(scope.pending))
        document = scope.pending[key]
        if not isinstance(document, _Removed) and not _same_document(_disk_text(key), document):
            ensure_lockfile_writable(key)
            atomic_write_text(key, document, new_file_mode=umask_file_mode(key))
        del scope.pending[key]
