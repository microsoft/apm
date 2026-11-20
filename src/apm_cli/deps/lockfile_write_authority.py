"""Single owner of when ``apm.lock.yaml`` reaches disk (#4086).

``LockFile.write`` decides *what* a lockfile holds (generator stamp, legacy
timestamp, no-op detection); this module decides *when* those bytes are
persisted, and by whom.

The writers form a closed set: each :class:`LockfileWriter` names one command
entry point, which declares itself with :func:`lockfile_writer`.  A command
refuses a lockfile it may not rewrite before it records anything, so its write
scope opens only at its first lockfile write.  Inside the scope every
``LockFile.write`` is staged in memory, every ``LockFile.read`` (and
:func:`lockfile_exists`) sees the staged document, and the command commits
the final document once, when it completes.  A command therefore writes the
lockfile at most once, however many phases record state in it.  Nested
writers (``apm update`` running the install service) join the outer one.

A command commits what it staged when it completes, and also when it fails
after changing state: an install that deployed files and then exits non-zero
(a later MCP conflict, a native write failure) keeps the lockfile records of
what reached disk.  Most refusals happen before the first write, so nothing
is staged to commit.  A check refusal raised after staging (an exception
class with ``refuses_commit = True``, such as an org-policy or install-time
audit block, anywhere in the error's cause chain), an interrupt (Ctrl-C, or
SIGTERM, which the CLI raises as one) or a
``sys.exit`` with a non-zero code discards what was staged.  Two explicit commit points
flush earlier: :func:`flush_lockfile_writes` runs before state outside APM's
transaction must agree with the lockfile (native plugin registration, user
lifecycle scripts, a command's own durable checkpoint).  State bound to the
committed lockfile waits for the commit through :func:`after_lockfile_commit`.  Removal and rollback
go through :func:`remove_lockfile` and :func:`restore_lockfile_bytes` so a
staged document never resurrects a lockfile a writer deleted or restored.

Staged documents are visible only inside this process; serializing commands
across processes stays with the lifecycle lock (``install/locking.py``).
Outside a writer (library callers, unit tests) ``LockFile.write`` persists
immediately.  ``--frozen`` suppression is decided by ``LockFile.write`` before
it reaches this module, so withheld writes are never staged or flushed.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import ParamSpec, TypeVar

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
    """What one writer has staged since its first lockfile write."""

    writer: LockfileWriter
    pending: dict[Path, str | _Removed] = field(default_factory=dict)
    after_commit: list[Callable[[], None]] = field(default_factory=list)


@dataclass
class _Writer:
    writer: LockfileWriter
    scope: _WriteScope | None = None


_active_writer: ContextVar[_Writer | None] = ContextVar("apm_lockfile_writer", default=None)


def _key(path: Path) -> Path:
    # Absolute, not resolved: a symlinked lockfile must stay a symlink so the
    # writer can refuse it, and a cwd change later in the command must not
    # move the destination.
    return Path(os.path.abspath(path))


def _scope(*, open_: bool = False) -> _WriteScope | None:
    active = _active_writer.get()
    if active is None:
        return None
    if active.scope is None and open_:
        active.scope = _WriteScope(active.writer)
    return active.scope


def _refused(error: BaseException) -> bool:
    """Whether *error*, or an error in its cause/context chain, is a check refusal.

    A refusal (an exception class with ``refuses_commit = True``, such as an
    org-policy block) means the command must commit nothing (#4085).
    """
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(type(current), "refuses_commit", False):
            return True
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)
    return False


def _commits(error: BaseException) -> bool:
    """Whether a command ending in *error* still commits what it staged.

    A failure after a state change records that change; a check refusal, an
    interrupt or a non-zero ``sys.exit`` abandons the command, and a zero exit
    completes it.
    """
    if _refused(error):
        return False
    if isinstance(error, SystemExit | _click_exit()):
        return getattr(error, "exit_code", getattr(error, "code", 1)) in (0, None)
    return isinstance(error, Exception)


def _click_exit() -> type[BaseException]:
    from click.exceptions import Exit

    return Exit


@contextmanager
def lockfile_write_scope(writer: LockfileWriter) -> Iterator[None]:
    """Run as *writer*: stage its lockfile writes, commit them once on success."""
    if not isinstance(writer, LockfileWriter):
        raise TypeError(f"{writer!r} is not a registered lockfile writer")
    if _active_writer.get() is not None:
        yield
        return
    active = _Writer(writer)
    token = _active_writer.set(active)
    try:
        yield
    except BaseException as error:
        if active.scope is not None and _commits(error):
            _commit(active.scope)
        raise
    else:
        if active.scope is not None:
            commit_point_seam_for_tests()
            _commit(active.scope)
    finally:
        _active_writer.reset(token)


_seam_held = False


def commit_point_seam_for_tests() -> None:
    """Installed-binary fault seam at a command's commit point.

    ``APM_TEST_LOCK_COMMIT=hold:<marker>`` writes *marker* and waits (once per
    process) so a test can signal the command after it changed state and
    staged its lockfile, before it commits; ``APM_TEST_LOCK_COMMIT=kill`` ends
    the process there, as a killed process would, with nothing committed.
    Install reaches it before the transaction releases its deploy snapshot.
    """
    global _seam_held
    seam = os.environ.get("APM_TEST_LOCK_COMMIT", "")
    if seam == "kill":
        os._exit(137)
    if seam.startswith("hold:") and not _seam_held:
        import time

        _seam_held = True
        Path(seam[len("hold:") :]).write_text("staged\n", encoding="ascii")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            time.sleep(0.05)


def lockfile_writer(writer: LockfileWriter) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Declare a command entry point as the lockfile writer *writer*."""

    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(function)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with lockfile_write_scope(writer):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def stage_lockfile(path: Path, serialized: str) -> bool:
    """Stage *serialized* for *path* inside a writer; ``False`` outside one.

    ``LockFile.write`` already raised any refusal (symlinked or
    newer-generator lockfile) at the call site.
    """
    scope = _scope(open_=True)
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
    """Return the document this command staged for *path*, else its on-disk text.

    ``None`` means no lockfile: never written, or removed by this command.
    """
    scope = _scope()
    if scope is not None:
        staged = scope.pending.get(_key(path))
        if isinstance(staged, _Removed):
            return None
        if staged is not None:
            return staged
    return _disk_text(path)


def has_staged_lockfile(path: Path) -> bool | None:
    """``True``/``False`` when this command staged or removed *path*; else ``None``."""
    scope = _scope()
    if scope is None:
        return None
    staged = scope.pending.get(_key(path))
    if staged is None:
        return None
    return not isinstance(staged, _Removed)


def lockfile_exists(path: Path) -> bool:
    """Whether *path* holds a lockfile, counting what this command staged."""
    staged = has_staged_lockfile(path)
    return path.exists() if staged is None else staged


def remove_lockfile(path: Path) -> None:
    """Delete the lockfile at *path* and anything staged for it."""
    scope = _scope(open_=True)
    if scope is not None:
        key = _key(path)
        scope.pending.pop(key, None)
        scope.pending[key] = _REMOVED
    path.unlink(missing_ok=True)


def restore_lockfile_bytes(path: Path, data: bytes) -> None:
    """Roll *path* back to *data*, discarding anything staged for it."""
    from ..utils.atomic_io import atomic_write_text

    scope = _scope()
    if scope is not None:
        scope.pending.pop(_key(path), None)
    atomic_write_text(path, data.decode("utf-8"), normalize_line_endings=False)


@contextmanager
def lockfile_scratch_writes() -> Iterator[None]:
    """Write a private scratch snapshot now, outside the command's lockfile.

    A replay renders a copy of the lockfile into a scratch tree it reads back
    at once; that copy is never the project's lockfile, so it is neither
    staged nor withheld by ``--frozen``.
    """
    from .lockfile import _suppressed_writes

    writer = _active_writer.set(None)
    suppressed = _suppressed_writes.set(None)
    try:
        yield
    finally:
        _suppressed_writes.reset(suppressed)
        _active_writer.reset(writer)


def after_lockfile_commit(action: Callable[[], None]) -> None:
    """Run *action* once what this command staged is on disk.

    State bound to the committed lockfile (deployment receipt claims) waits
    for the commit; a discarded or failed commit never runs it. With nothing
    staged, the lockfile on disk is already the committed one.
    """
    scope = _scope()
    if scope is None or not scope.pending:
        action()
        return
    scope.after_commit.append(action)


def flush_lockfile_writes() -> None:
    """Commit what this command staged now; it keeps staging afterwards."""
    scope = _scope()
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
    actions, scope.after_commit = scope.after_commit, []
    for action in actions:
        action()
