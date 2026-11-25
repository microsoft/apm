"""RESEARCH ONLY (Lane N oracle O-walk): one directory listing per path per invocation.

During integrate, os.scandir/os.listdir of paths under apm_modules are served from the
first listing of that path (package trees are complete before integrate starts).
"""

from __future__ import annotations

import os

_ORIG_SCANDIR = os.scandir
_ORIG_LISTDIR = os.listdir
_SCANS: dict[str, list] = {}
STATS = {"scan_hit": 0, "scan_miss": 0, "list_hit": 0, "list_miss": 0, "inactive": 0}
_LISTS: dict[str, list] = {}
ACTIVE = False


def _key(path) -> str | None:
    if not ACTIVE or isinstance(path, int) or path is None:
        return None
    text = os.fspath(path)
    if isinstance(text, bytes):
        return None
    sep = os.sep
    if f"{sep}apm_modules{sep}" not in text and not text.endswith(f"{sep}apm_modules"):
        return None
    return os.path.abspath(text)


class _Cached:
    def __init__(self, entries):
        self._it = iter(entries)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def _scandir(path="."):
    key = _key(path)
    if key is None:
        STATS["inactive"] += 1
        return _ORIG_SCANDIR(path)
    entries = _SCANS.get(key)
    STATS["scan_hit" if entries is not None else "scan_miss"] += 1
    if entries is None:
        with _ORIG_SCANDIR(path) as it:
            entries = _SCANS[key] = list(it)
    return _Cached(entries)


def _listdir(path="."):
    key = _key(path)
    if key is None:
        return _ORIG_LISTDIR(path)
    names = _LISTS.get(key)
    STATS["list_hit" if names is not None else "list_miss"] += 1
    if names is None:
        names = _LISTS[key] = _ORIG_LISTDIR(path)
    return list(names)


def install() -> None:
    import atexit
    import sys

    atexit.register(lambda: sys.stderr.write(f"[owalk] {STATS}\n"))
    os.scandir = _scandir
    os.listdir = _listdir
