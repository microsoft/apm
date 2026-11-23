"""Research hook: count path operations per (op, path, apm_cli caller) for one command.

Usage: python path_attr.py OUT_JSON APM_SCRIPT ARGV...

Counts only the outermost path op (an lstat inside realpath is folded into realpath)
and times the original call, excluding this hook's bookkeeping. A repeat of the same
(op, path) is "faithfully dedupable" when nothing that could change its result ran
since the previous identical op: no write-mode open, rename, replace, unlink, rmdir,
mkdir, chmod, symlink, link, utime or child process anywhere in the process (a
deliberately conservative global generation). relative_to is pure, so every repeat
with the same (path, base) is dedupable.
"""

from __future__ import annotations

import atexit
import builtins
import io
import json
import os
import pathlib
import posixpath
import runpy
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict

OUT = sys.argv[1]
APM = sys.argv[2]
_tls = threading.local()
_lock = threading.Lock()
_gen = [0]
_calls: Counter = Counter()  # (op, caller) -> calls
_secs: defaultdict = defaultdict(float)  # (op, caller) -> seconds
_dup: Counter = Counter()  # (op, caller) -> faithful repeats
_dup_secs: defaultdict = defaultdict(float)
_paths: Counter = Counter()  # (op, path) -> calls
_last_gen: dict = {}  # (op, key) -> generation of last call
_inner: Counter = Counter()  # nested ops (inside another counted op)
_mutations: Counter = Counter()
_SRC = os.sep + "apm_cli" + os.sep


def _caller() -> str:
    frame = sys._getframe(2)
    while frame is not None:
        name = frame.f_code.co_filename
        if _SRC in name:
            mod = name.rsplit(_SRC, 1)[1]
            return f"{mod}:{frame.f_code.co_name}"
        frame = frame.f_back
    return "<outside apm_cli>"


def _counted(op: str, original, key_of, pure: bool = False):
    def wrapper(*args, **kwargs):
        if getattr(_tls, "depth", 0):
            _inner[op] += 1
            return original(*args, **kwargs)
        _tls.depth = 1
        try:
            caller = _caller()
            try:
                key = key_of(*args, **kwargs)
            except Exception:
                key = "?"
            gen = _gen[0]
            start = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                spent = time.perf_counter() - start
                with _lock:
                    _calls[(op, caller)] += 1
                    _secs[(op, caller)] += spent
                    _paths[(op, key)] += 1
                    prev = _last_gen.get((op, key))
                    if prev is not None and (pure or prev == gen):
                        _dup[(op, caller)] += 1
                        _dup_secs[(op, caller)] += spent
                    _last_gen[(op, key)] = gen
        finally:
            _tls.depth = 0

    return wrapper


def _mutating(op: str, original):
    def wrapper(*args, **kwargs):
        _gen[0] += 1
        _mutations[op] += 1
        return original(*args, **kwargs)

    return wrapper


def _first(*args, **_kwargs):
    return os.fspath(args[0]) if args else "?"


def _self_and_base(self, *others, **_kwargs):
    return f"{self}\0{'|'.join(map(str, others))}"


os.lstat = _counted("lstat", os.lstat, _first)
os.stat = _counted("stat", os.stat, _first)
os.path.realpath = posixpath.realpath = _counted("realpath", posixpath.realpath, _first)
os.scandir = _counted("scandir", os.scandir, lambda *a, **k: os.fspath(a[0]) if a else ".")
pathlib.Path.resolve = _counted("resolve", pathlib.Path.resolve, lambda self, *a, **k: str(self))
pathlib.PurePath.relative_to = _counted(
    "relative_to", pathlib.PurePath.relative_to, _self_and_base, pure=True
)
for _name in ("rename", "replace", "unlink", "remove", "rmdir", "mkdir", "chmod",
              "symlink", "link", "utime", "makedirs"):
    setattr(os, _name, _mutating(_name, getattr(os, _name)))
_orig_open = builtins.open


def _open(file, mode="r", *args, **kwargs):
    if any(c in mode for c in "wax+"):
        _gen[0] += 1
        _mutations["open-write"] += 1
    return _orig_open(file, mode, *args, **kwargs)


builtins.open = io.open = _open
_orig_os_open = os.open


def _os_open(path, flags, *args, **kwargs):
    if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
        _gen[0] += 1
        _mutations["os.open-write"] += 1
    return _orig_os_open(path, flags, *args, **kwargs)


os.open = _os_open
_orig_popen = subprocess.Popen.__init__


def _popen(self, *args, **kwargs):
    _gen[0] += 1
    _mutations["spawn"] += 1
    return _orig_popen(self, *args, **kwargs)


subprocess.Popen.__init__ = _popen


def _dump() -> None:
    ops = sorted({op for op, _ in _calls})
    per_op = {
        op: {
            "calls": sum(n for (o, _), n in _calls.items() if o == op),
            "unique": sum(1 for (o, _) in _paths if o == op),
            "faithful_dups": sum(n for (o, _), n in _dup.items() if o == op),
            "secs": round(sum(s for (o, _), s in _secs.items() if o == op), 4),
            "faithful_dup_secs": round(sum(s for (o, _), s in _dup_secs.items() if o == op), 4),
        }
        for op in ops
    }
    callers = sorted(_calls, key=lambda k: -_dup_secs[k])[:40]
    report = {
        "per_op": per_op,
        "inner": dict(_inner),
        "mutations": dict(_mutations),
        "callers": [
            {"op": op, "caller": c, "calls": _calls[(op, c)], "dups": _dup[(op, c)],
             "secs": round(_secs[(op, c)], 4), "dup_secs": round(_dup_secs[(op, c)], 4)}
            for op, c in callers
        ],
        "top_paths": [
            {"op": op, "path": p[-160:], "calls": n} for (op, p), n in _paths.most_common(30)
        ],
    }
    with _orig_open(OUT, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1)


atexit.register(_dump)
sys.argv = [APM, *sys.argv[3:]]
runpy.run_path(APM, run_name="__main__")
