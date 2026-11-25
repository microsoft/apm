"""RESEARCH ONLY (Lane N bound O-time): CPU inside stdlib walk/stat primitives, whole run.

Times the outermost call (reentrancy guard) of every walk/list/glob/stat primitive; generators are
timed per next(). The sum is the most any native walk/stat layer could remove (native >= 0).
"""

from __future__ import annotations

import os
import os.path
import pathlib
import time

STATS: dict[str, float] = {}
COUNTS: dict[str, int] = {}
_depth = [0]


def _add(name: str, dt: float) -> None:
    STATS[name] = STATS.get(name, 0.0) + dt
    COUNTS[name] = COUNTS.get(name, 0) + 1


def _timed(name, fn):
    def wrapper(*args, **kwargs):
        if _depth[0]:
            return fn(*args, **kwargs)
        _depth[0] += 1
        start = time.thread_time()
        try:
            return fn(*args, **kwargs)
        finally:
            _add(name, time.thread_time() - start)
            _depth[0] -= 1

    return wrapper


def _timed_iter(name, fn):
    def wrapper(*args, **kwargs):
        if _depth[0]:
            return fn(*args, **kwargs)
        _depth[0] += 1
        start = time.thread_time()
        try:
            it = fn(*args, **kwargs)
        finally:
            _add(name, time.thread_time() - start)
            _depth[0] -= 1
        return _Iter(name, it)

    return wrapper


class _Iter:
    def __init__(self, name, it):
        self._name, self._it = name, it

    def __iter__(self):
        return self

    def __next__(self):
        if _depth[0]:
            return next(self._it)
        _depth[0] += 1
        start = time.thread_time()
        try:
            return next(self._it)
        finally:
            _add(self._name + ".next", time.thread_time() - start)
            _depth[0] -= 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        close = getattr(self._it, "close", None)
        if close:
            close()
        return False

    def close(self):
        close = getattr(self._it, "close", None)
        if close:
            close()


def install() -> None:
    import atexit
    import sys

    os.walk = _timed_iter("walk", os.walk)
    os.scandir = _timed_iter("scandir", os.scandir)
    os.listdir = _timed("listdir", os.listdir)
    os.stat = _timed("stat", os.stat)
    os.lstat = _timed("lstat", os.lstat)
    for name in ("exists", "isfile", "isdir", "islink", "lexists"):
        setattr(os.path, name, _timed(f"path.{name}", getattr(os.path, name)))
    P = pathlib.Path
    for name in ("glob", "rglob", "iterdir"):
        setattr(P, name, _timed_iter(f"Path.{name}", getattr(P, name)))
    for name in ("stat", "lstat", "exists", "is_file", "is_dir", "is_symlink"):
        setattr(P, name, _timed(f"Path.{name}", getattr(P, name)))

    def report():
        total = sum(STATS.values())
        top = sorted(STATS.items(), key=lambda kv: -kv[1])
        body = ", ".join(f"{k} {v:.3f}s/{COUNTS[k]}" for k, v in top)
        sys.stderr.write(f"[oracle-otime] total {total:.3f}s cpu_main_thread {time.thread_time():.3f}s proc {time.process_time():.3f}s | {body}\n")

    atexit.register(report)
