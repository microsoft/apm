"""Cross-process serialization for one mutable APM workspace."""

from __future__ import annotations

import contextlib
import inspect
import os
import threading
import weakref
from collections.abc import Callable, Iterator
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar

from filelock import FileLock, Timeout

from apm_cli.utils.path_security import has_symlink_component

LIFECYCLE_LOCK_TIMEOUT = 120.0
_LIFECYCLE_LOCK_NAME = ".apm-lifecycle.lock"

_locks: weakref.WeakValueDictionary[tuple[int, Path], FileLock] = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()
_T = TypeVar("_T")


class LifecycleBusyError(RuntimeError):
    """Raised when another APM state mutation owns the lifecycle lock."""


def _validate_lifecycle_lock_path(lock_path: Path) -> None:
    """Reject lock paths that traverse a symlink below the user's home."""
    home = Path.home()
    if has_symlink_component(home, lock_path):
        raise LifecycleBusyError(
            f"Refusing symlinked lifecycle lock path: {lock_path}. "
            "Replace the symlink with a regular directory or file, then retry."
        )


def lifecycle_lock() -> FileLock:
    """Return the process-reentrant OS-user lifecycle lock."""
    lock_path = Path.home() / ".apm" / _LIFECYCLE_LOCK_NAME
    _validate_lifecycle_lock_path(lock_path)
    key = (os.getpid(), lock_path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = FileLock(str(lock_path), timeout=LIFECYCLE_LOCK_TIMEOUT)
            _locks[key] = lock
        return lock


def acquire_lifecycle_lock(*, timeout: float = LIFECYCLE_LOCK_TIMEOUT) -> FileLock:
    """Acquire the lifecycle lock or raise a clear busy-workspace error."""
    lock = lifecycle_lock()
    Path(lock.lock_file).parent.mkdir(parents=True, exist_ok=True)
    _validate_lifecycle_lock_path(Path(lock.lock_file))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        from apm_cli.utils.console import _rich_info

        _rich_info(
            f"Another APM operation is running; waiting up to {timeout:g}s.",
            symbol="info",
        )
        try:
            lock.acquire(timeout=timeout)
        except Timeout as wait_exc:
            raise LifecycleBusyError(
                f"Another APM operation is still running; waited {timeout:g}s. "
                "Wait for it to finish or stop it, then retry. "
                f"Lifecycle lock: {lock.lock_file}"
            ) from wait_exc
    return lock


@contextlib.contextmanager
def lifecycle_operation() -> Iterator[None]:
    """Serialize one complete APM state mutation."""
    lock = acquire_lifecycle_lock()
    try:
        yield
    finally:
        lock.release()


def serialized_lifecycle(run: Callable[..., _T]) -> Callable[..., _T]:
    """Serialize a command callback before its first state read."""

    @wraps(run)
    def wrapped(*args: Any, **kwargs: Any) -> _T:
        with lifecycle_operation():
            return run(*args, **kwargs)

    return wrapped


def serialized_lifecycle_unless(
    read_only_argument: str,
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """Serialize a callback unless its named read-only option is true."""

    def decorate(run: Callable[..., _T]) -> Callable[..., _T]:
        signature = inspect.signature(run)

        @wraps(run)
        def wrapped(*args: Any, **kwargs: Any) -> _T:
            arguments = signature.bind_partial(*args, **kwargs).arguments
            if arguments.get(read_only_argument, False):
                return run(*args, **kwargs)
            with lifecycle_operation():
                return run(*args, **kwargs)

        return wrapped

    return decorate


def serialized_lifecycle_when(
    mutating_argument: str,
    *,
    unless_argument: str | None = None,
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """Serialize a callback only for its state-mutating option path."""

    def decorate(run: Callable[..., _T]) -> Callable[..., _T]:
        signature = inspect.signature(run)

        @wraps(run)
        def wrapped(*args: Any, **kwargs: Any) -> _T:
            arguments = signature.bind_partial(*args, **kwargs).arguments
            mutates = bool(arguments.get(mutating_argument, False))
            exempt = bool(unless_argument and arguments.get(unless_argument, False))
            if not mutates or exempt:
                return run(*args, **kwargs)
            with lifecycle_operation():
                return run(*args, **kwargs)

        return wrapped

    return decorate
