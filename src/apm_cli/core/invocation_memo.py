"""Per-invocation memos: one command's repeated, identical lookups are answered once.

A memo exists only inside :func:`invocation_scope`, which the root CLI group opens
for each command, so nothing outlives one invocation and direct API callers (and
tests that call owners without the CLI) see no memo at all. The state is a
lock-guarded module global rather than a context variable, so worker threads of
the same invocation share it.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from apm_cli.utils.realpath_win import release_held

_T = TypeVar("_T")

_lock = threading.Lock()
_memos: dict[str, dict[Any, Any]] | None = None
_depth = 0
_overlapped = False
_generation = 0  # bumped whenever memos are invalidated; a stale read never publishes
_forgotten = 0  # bumped only when user code ran mid-command (forget_invocation_memos)
_KEY_LOCK = object()  # memo key prefix for the per-key lock of a memoized call


@contextmanager
def invocation_scope() -> Iterator[None]:
    """Give the enclosed command a fresh set of memos and drop them on exit.

    One CLI process runs one command, so scopes never overlap in practice. If
    they do (threads or nested runners), memoization is off for everyone until
    the last scope exits, so no invocation reads another's memo and no state
    survives the scopes.
    """
    global _memos, _depth, _overlapped, _generation
    with _lock:
        _generation += 1
        _depth += 1
        if _depth == 1:
            _memos, _overlapped = {}, False
        else:
            _memos, _overlapped = None, True
    if _overlapped:
        release_held()  # holds are per invocation: none while scopes overlap
    try:
        yield
    finally:
        with _lock:
            _generation += 1
            _depth -= 1
            if _depth == 0:
                _memos, _overlapped = None, False
        release_held()


def invocation_active() -> bool:
    """Whether exactly one invocation scope is open (per-invocation state is allowed)."""
    with _lock:
        return _memos is not None and not _overlapped


def invocation_memo(name: str) -> dict[Any, Any] | None:
    """Return the named memo of the current invocation, or ``None`` outside one."""
    with _lock:
        if _memos is None or _overlapped:
            return None
        return _memos.setdefault(name, {})


def forget_invocation_memos() -> None:
    """Drop every memo of the current invocation, keeping its scope open.

    Called after user code (a lifecycle command script) runs mid-command: it
    may have refreshed credentials or changed the git remote, so nothing
    learned before it may answer a later lookup. Held directories are released
    too: user code may rename or delete what APM held.
    """
    global _generation, _forgotten
    with _lock:
        _generation += 1
        _forgotten += 1
        if _memos is not None:
            for memo in _memos.values():
                memo.clear()  # a reference captured before the script sees nothing
            _memos.clear()
    release_held()


def forget_epoch() -> int:
    """How many times user code invalidated what this process learned.

    A reader that keeps an answer past its invocation (policy/_remote.py's
    record-render reuse) stamps it with this and drops it when it changes.
    """
    with _lock:
        return _forgotten


def memoized_per_invocation(
    name: str, *, keep: Callable[[Any], bool]
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """Reuse a result of *fn* within one invocation when ``keep(result)`` is true.

    Calls with the same key run one at a time, so concurrent workers that miss
    together fetch once; other keys proceed in parallel. Results ``keep``
    rejects (for example errors that a retry may clear) are recomputed on every
    call, exactly as without the memo.
    """

    def decorate(fn: Callable[..., _T]) -> Callable[..., _T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> _T:
            key = (args, tuple(sorted(kwargs.items())))
            with _lock:
                generation = _generation
                memo = None if _memos is None or _overlapped else _memos.setdefault(name, {})
                key_lock = (
                    None if memo is None else memo.setdefault((_KEY_LOCK, key), threading.Lock())
                )
            if memo is None or key_lock is None:
                return fn(*args, **kwargs)
            with key_lock:
                with _lock:
                    if generation == _generation and key in memo:
                        return memo[key]
                result = fn(*args, **kwargs)
                with _lock:
                    # Publish only if nothing invalidated the memo meanwhile.
                    if generation == _generation and keep(result):
                        memo[key] = result
                return result

        return wrapper

    return decorate
