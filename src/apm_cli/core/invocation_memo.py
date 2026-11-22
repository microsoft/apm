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

_T = TypeVar("_T")

_lock = threading.Lock()
_memos: dict[str, dict[Any, Any]] | None = None


@contextmanager
def invocation_scope() -> Iterator[None]:
    """Give the enclosed command a fresh set of memos and drop them on exit."""
    global _memos
    with _lock:
        previous, _memos = _memos, {}
    try:
        yield
    finally:
        with _lock:
            _memos = previous


def invocation_memo(name: str) -> dict[Any, Any] | None:
    """Return the named memo of the current invocation, or ``None`` outside one."""
    with _lock:
        if _memos is None:
            return None
        return _memos.setdefault(name, {})


def memoized_per_invocation(
    name: str, *, keep: Callable[[Any], bool]
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """Reuse a result of *fn* within one invocation when ``keep(result)`` is true.

    Results ``keep`` rejects (for example errors that a retry may clear) are
    recomputed on every call, exactly as without the memo.
    """

    def decorate(fn: Callable[..., _T]) -> Callable[..., _T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> _T:
            memo = invocation_memo(name)
            key = (args, tuple(sorted(kwargs.items())))
            if memo is not None and key in memo:
                return memo[key]
            result = fn(*args, **kwargs)
            if memo is not None and keep(result):
                memo[key] = result
            return result

        return wrapper

    return decorate
