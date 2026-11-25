"""Research only (lane-N B): unsafe per-invocation result memos for redundant passes.

Each decorated owner answers once per (owner, args) inside one CLI invocation,
and only for paths under ``apm_modules`` (written once, then read-only on a cold
fresh install). Not product code: it establishes a deletion ceiling.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from typing import Any

from apm_cli.core.invocation_memo import invocation_memo

STATS = {"hit": 0, "miss": 0}


def _in_modules(value: Any) -> bool:
    try:
        return "apm_modules" in os.fspath(value).replace("\\", "/").split("/")
    except TypeError:
        return False


def passmemo(name: str, *, path_arg: int = 0, copy: Callable[[Any], Any] | None = None):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            memo = invocation_memo("research-passmemo")
            if memo is None or len(args) <= path_arg or not _in_modules(args[path_arg]):
                return fn(*args, **kwargs)
            key = (
                name,
                tuple(str(a) for a in args),
                tuple(sorted((k, str(v)) for k, v in kwargs.items())),
            )
            if key in memo:
                STATS["hit"] += 1
                value = memo[key]
            else:
                STATS["miss"] += 1
                value = memo[key] = fn(*args, **kwargs)
            return copy(value) if copy else value

        return wrapper

    return deco
