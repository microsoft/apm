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

from apm_cli.core.deployment_ledger import LOCAL_STATE_DIR
from apm_cli.core.scope import get_apm_home
from apm_cli.utils.path_security import has_symlink_component

LIFECYCLE_LOCK_TIMEOUT = 120.0
_LIFECYCLE_LOCK_NAME = ".apm-lifecycle.lock"

_locks: weakref.WeakValueDictionary[tuple[int, Path], FileLock] = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()
_T = TypeVar("_T")


class LifecycleBusyError(RuntimeError):
    """Raised when another APM state mutation owns the lifecycle lock."""


def _reject_symlinked_lock(base: Path, lock_path: Path) -> None:
    if has_symlink_component(base, lock_path.absolute()):
        raise LifecycleBusyError(
            f"Refusing symlinked lifecycle lock path: {lock_path}. "
            "Replace the symlink with a regular directory or file, then retry."
        )


def _validate_lifecycle_lock_path(lock_path: Path) -> None:
    """Reject lock paths that traverse a symlink into the APM metadata root."""
    _reject_symlinked_lock(get_apm_home().absolute().parent, lock_path)


def _process_lock(lock_path: Path) -> FileLock:
    key = (os.getpid(), lock_path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = FileLock(str(lock_path), timeout=LIFECYCLE_LOCK_TIMEOUT)
            _locks[key] = lock
        return lock


def lifecycle_lock() -> FileLock:
    """Return the process-reentrant lock for APM metadata under ``APM_HOME``."""
    lock_path = get_apm_home() / _LIFECYCLE_LOCK_NAME
    _validate_lifecycle_lock_path(lock_path)
    return _process_lock(lock_path)


def deployment_root_lock() -> FileLock:
    """Return the process-reentrant OS-user lock that serializes every ``APM_HOME``.

    Processes with different ``APM_HOME`` values still share projects, harness
    directories and the ``--global`` installation identity under the OS home,
    so they must not mutate concurrently.  The lock lives beside that identity
    in ``~/.apm-local/`` rather than in any APM home.
    """
    home = Path.home()
    lock_path = home / LOCAL_STATE_DIR / _LIFECYCLE_LOCK_NAME
    _reject_symlinked_lock(home.absolute(), lock_path)
    return _process_lock(lock_path)


class LifecycleLockHandle:
    """One acquisition of the deployment-root and metadata locks, released together."""

    def __init__(self, deployment: FileLock, metadata: FileLock) -> None:
        self._deployment = deployment
        self._metadata = metadata
        self.lock_file = metadata.lock_file

    @property
    def is_locked(self) -> bool:
        return self._metadata.is_locked

    @property
    def lock_counter(self) -> int:
        return self._metadata.lock_counter

    def release(self) -> None:
        """Release the metadata lock, then the deployment-root lock."""
        try:
            self._metadata.release()
        finally:
            self._deployment.release()


def _acquire_or_wait(lock: FileLock, timeout: float, validate: Callable[[Path], None]) -> None:
    Path(lock.lock_file).parent.mkdir(parents=True, exist_ok=True)
    validate(Path(lock.lock_file))
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


def acquire_lifecycle_lock(*, timeout: float = LIFECYCLE_LOCK_TIMEOUT) -> LifecycleLockHandle:
    """Acquire the deployment-root lock, then the metadata lock, or raise a busy error."""
    deployment = deployment_root_lock()
    metadata = lifecycle_lock()
    home = Path.home().absolute()
    _acquire_or_wait(deployment, timeout, lambda path: _reject_symlinked_lock(home, path))
    try:
        _acquire_or_wait(metadata, timeout, _validate_lifecycle_lock_path)
    except BaseException:
        deployment.release()
        raise
    return LifecycleLockHandle(deployment, metadata)


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
