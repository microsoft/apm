"""RESEARCH ONLY (Lane N oracle O-walk): one directory listing per path per invocation.

During integrate, os.scandir/os.listdir of paths under apm_modules are served from the
first listing of that path (package trees are complete before integrate starts).
"""

from __future__ import annotations

import os

_ORIG_SCANDIR = os.scandir
_ORIG_LISTDIR = os.listdir
_SCANS: dict[str, list] = {}
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
        return _ORIG_SCANDIR(path)
    entries = _SCANS.get(key)
    if entries is None:
        with _ORIG_SCANDIR(path) as it:
            entries = _SCANS[key] = list(it)
    return _Cached(entries)


def _listdir(path="."):
    key = _key(path)
    if key is None:
        return _ORIG_LISTDIR(path)
    names = _LISTS.get(key)
    if names is None:
        names = _LISTS[key] = _ORIG_LISTDIR(path)
    return list(names)


def install() -> None:
    os.scandir = _scandir
    os.listdir = _listdir
    install_walk2()


# --- O-walk2: zero-cost os.walk / Path.glob / Path.rglob for apm_modules trees -----------------
import pathlib as _pathlib

_ORIG_WALK = os.walk
_ORIG_GLOB = _pathlib.Path.glob
_ORIG_RGLOB = _pathlib.Path.rglob
_TREES: dict[str, dict] = {}
_GLOBS: dict[tuple, list] = {}


def _walk(top, topdown=True, onerror=None, followlinks=False):
    key = _key(top) if topdown and not followlinks else None
    if key is None:
        yield from _ORIG_WALK(top, topdown, onerror, followlinks)
        return
    form = os.fspath(top)
    tree = _TREES.get(form)
    if tree is None:
        tree = {root: (dirs, files) for root, dirs, files in _ORIG_WALK(form, True, onerror, False)}
        _TREES[form] = tree
    stack = [form]
    while stack:
        root = stack.pop()
        entry = tree.get(root)
        if entry is None:
            continue
        dirs = list(entry[0])
        yield root, dirs, list(entry[1])
        for name in reversed(dirs):
            stack.append(os.path.join(root, name))


def _glob_cached(orig, kind):
    def method(self, pattern, *args, **kwargs):
        key = _key(self)
        if key is None or args or kwargs:
            return orig(self, pattern, *args, **kwargs)
        ck = (kind, str(self), pattern)
        found = _GLOBS.get(ck)
        if found is None:
            found = _GLOBS[ck] = list(orig(self, pattern))
        return iter(found)

    return method


def install_walk2() -> None:
    os.walk = _walk
    _pathlib.Path.glob = _glob_cached(_ORIG_GLOB, "glob")
    _pathlib.Path.rglob = _glob_cached(_ORIG_RGLOB, "rglob")
