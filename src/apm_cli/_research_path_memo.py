"""RESEARCH ONLY (never merge): faithful per-command memo of realpath and relative_to.

realpath results only change when a path component is removed, renamed or
replaced, or becomes a symlink, or the cwd changes. Every such mutation made
by this process flushes the whole memo. relative_to is a pure function of its
operands. Counts go to APM_PATH_MEMO_LOG at exit.
"""

import atexit
import json
import os
import pathlib
import posixpath
import shutil

_memo = {}
_rel = {}
_stats = {"realpath_hit": 0, "realpath_miss": 0, "rel_hit": 0, "rel_miss": 0, "flush": 0}
_real = posixpath.realpath


def _flush():
    _stats["flush"] += 1
    _memo.clear()


def _realpath(path, *args, strict=False, **kw):
    if args or kw:
        return _real(path, *args, strict=strict, **kw)
    raw = os.fspath(path)
    key = (raw, strict, None if os.path.isabs(raw) else os.getcwd())
    try:
        hit = _memo[key]
    except KeyError:
        pass
    except TypeError:
        return _real(path, strict=strict)
    else:
        _stats["realpath_hit"] += 1
        return hit if isinstance(raw, type(hit)) else _real(path, strict=strict)
    _stats["realpath_miss"] += 1
    value = _real(path, strict=strict)
    _memo[key] = value
    return value


def _flushing(fn):
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        finally:
            _flush()

    wrapper.__wrapped__ = fn
    return wrapper


_relative_to = pathlib.PurePath.relative_to


def _rel_memo(self, other, *rest, **kw):
    if rest or kw:
        return _relative_to(self, other, *rest, **kw)
    try:
        key = (type(self), str(self), type(other), str(other))
        hit = _rel[key]
    except KeyError:
        pass
    except TypeError:
        return _relative_to(self, other)
    else:
        _stats["rel_hit"] += 1
        return hit
    _stats["rel_miss"] += 1
    value = _relative_to(self, other)
    _rel[key] = value
    return value


if os.name != "nt" and os.environ.get("APM_PATH_MEMO", "1") != "0":
    posixpath.realpath = _realpath
    for _name in (
        "unlink",
        "remove",
        "rmdir",
        "rename",
        "replace",
        "symlink",
        "chdir",
        "removedirs",
        "renames",
    ):
        if hasattr(os, _name):
            setattr(os, _name, _flushing(getattr(os, _name)))
    shutil.move = _flushing(shutil.move)
    # A child process (git clone/checkout) may create or replace links.
    import subprocess

    subprocess.Popen.__init__ = _flushing(subprocess.Popen.__init__)
    subprocess.Popen.wait = _flushing(subprocess.Popen.wait)
    _poll = subprocess.Popen.poll

    def _poll_flush(self):
        code = _poll(self)
        if code is not None:
            _flush()
        return code

    subprocess.Popen.poll = _poll_flush
    pathlib.PurePath.relative_to = _rel_memo
    _log = os.environ.get("APM_PATH_MEMO_LOG")
    if _log:

        def _dump():
            with open(_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"pid": os.getpid(), **_stats}) + "\n")

        atexit.register(_dump)
