"""RESEARCH ONLY (Lane N oracle O-resolve): every Path.resolve / os.path.realpath is lexical (counted)."""

from __future__ import annotations

import os
import pathlib

STATS = {"path_resolve": 0, "os_realpath": 0, "resolve_windows_native": 0}


def install() -> None:
    import atexit
    import sys

    def resolve(self, strict=False):
        STATS["path_resolve"] += 1
        return type(self)(os.path.abspath(self))

    def realpath(path, *a, **k):
        STATS["os_realpath"] += 1
        return os.path.abspath(path)

    pathlib.Path.resolve = resolve
    os.path.realpath = realpath
    from apm_cli.utils import realpath as _rp

    if hasattr(_rp, "_resolve_windows"):
        original = _rp._resolve_windows

        def counted(path):
            STATS["resolve_windows_native"] += 1
            return original(path)

        _rp._resolve_windows = counted
    fn = _rp.resolve_path
    tag = f"resolve_path={fn.__code__.co_filename.rsplit('/', 1)[-1]}:{fn.__code__.co_firstlineno}"
    atexit.register(lambda: sys.stderr.write(f"[oracle-oresolve] {STATS} {tag} platform={sys.platform}\\n"))
