"""RESEARCH ONLY (perf R3): string-level PurePath containment plus a resolve memo.

1. ``PurePath.relative_to`` / ``is_relative_to``: a pure string fast path for the
   common case (same class, both absolute, drive-letter or POSIX root, no walk_up),
   falling back to pathlib for everything else. No filesystem access either way.
2. ``Path.resolve`` is memoized ONLY for allowlisted non-security callers, keyed by
   a process-wide mutation generation (any write-mode open, rename, replace,
   unlink, rmdir, mkdir, chmod, symlink, link, utime, chdir, no-replace publish or
   child process bumps it). Every other caller, including utils/path_security.py,
   runs the original resolve: the same realpath and stat syscalls, in order.
"""

from __future__ import annotations

import builtins
import io
import ntpath
import os
import pathlib
import posixpath
import subprocess
import sys
import threading

_orig_relative_to = pathlib.PurePath.relative_to
_orig_is_relative_to = pathlib.PurePath.is_relative_to
_orig_resolve = pathlib.Path.resolve
_FALLBACK = object()


def _remainder(self, other):
    """Tail of *self* under *other* as a string, None if not under, _FALLBACK if unsure."""
    if type(other) is not type(self):
        return _FALLBACK
    flavour = self._flavour
    s = str(self)
    o = str(other)
    if flavour is posixpath:
        if s[:1] != "/" or o[:1] != "/" or s[:2] == "//" or o[:2] == "//":
            return _FALLBACK
        ns, no, sep = s, o, "/"
    elif flavour is ntpath:
        for text in (s, o):
            if len(text) < 3 or text[1:3] != ":\\" or not (text[0].isascii() and text[0].isalpha()):
                return _FALLBACK
        ns, no, sep = s.lower(), o.lower(), "\\"
        if len(ns) != len(s) or len(no) != len(o):
            return _FALLBACK
    else:
        return _FALLBACK
    if ns == no:
        return ""
    prefix = no if no.endswith(sep) else no + sep
    if not ns.startswith(prefix):
        return None
    rest = s[len(prefix) :]
    if flavour is ntpath and ":" in rest:
        return _FALLBACK
    return rest


def relative_to(self, other, /, *_deprecated, walk_up=False):
    if not _deprecated and not walk_up:
        rest = _remainder(self, other)
        if rest is not _FALLBACK:
            if rest is None:
                raise ValueError(f"{str(self)!r} is not in the subpath of {str(other)!r}")
            return self.with_segments(rest) if rest else self.with_segments()
    return _orig_relative_to(self, other, *_deprecated, walk_up=walk_up)


def is_relative_to(self, other, /, *_deprecated):
    if not _deprecated:
        rest = _remainder(self, other)
        if rest is not _FALLBACK:
            return rest is not None
    return _orig_is_relative_to(self, other, *_deprecated)


# --- resolve memo at allowlisted non-security callers --------------------------------

_ALLOW = {
    ("utils/paths.py", "portable_relpath"),
    ("deps/plugin_root_render.py", "bind_plugin_roots"),
    ("deps/plugin_parser.py", "resolve_plugin_root_placeholders"),
    ("integration/base_integrator.py", "find_files_by_glob"),
    ("integration/base_integrator.py", "init_link_resolver"),
    ("integration/base_integrator.py", "_is_root_local_package"),
    ("models/apm_package.py", "from_apm_yml"),
    ("models/apm_package.py", "from_mapping"),
    ("integration/skill_adoption.py", "_inactive_skill_roots"),
    ("models/validation.py", "validate_apm_package"),
    ("deps/apm_resolver.py", "_compute_dep_source_path"),
    ("compilation/inventory.py", "files_within"),
    ("primitives/discovery.py", "find_primitive_files"),
    ("integration/instruction_integrator.py", "_package_key"),
}
_SRC = os.sep + "apm_cli" + os.sep
_allowed_codes: dict = {}
_gen = [0]
_memo: dict = {}
_lock = threading.Lock()
STATS = {"hits": 0, "misses": 0, "bumps": 0}


def _allowed(code) -> bool:
    verdict = _allowed_codes.get(code)
    if verdict is None:
        name = code.co_filename
        rel = name.rsplit(_SRC, 1)[1].replace(os.sep, "/") if _SRC in name else ""
        verdict = (rel, code.co_name) in _ALLOW
        _allowed_codes[code] = verdict
    return verdict


def resolve(self, strict=False):
    if not _allowed(sys._getframe(1).f_code):
        return _orig_resolve(self, strict)
    gen = _gen[0]
    key = (type(self), str(self), strict)
    hit = _memo.get(key)
    if hit is not None and hit[0] == gen:
        STATS["hits"] += 1
        return hit[1]
    STATS["misses"] += 1
    result = _orig_resolve(self, strict)
    with _lock:
        _memo[key] = (gen, result)
    return result


def _bump() -> None:
    _gen[0] += 1
    STATS["bumps"] += 1


def _bumping(original):
    def wrapper(*args, **kwargs):
        _bump()
        try:
            return original(*args, **kwargs)
        finally:
            _bump()

    wrapper.__wrapped__ = original
    return wrapper


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | getattr(os, "O_APPEND", 0)


def install() -> None:
    pathlib.PurePath.relative_to = relative_to
    pathlib.PurePath.is_relative_to = is_relative_to
    # DIAGNOSTIC: string relative_to only, no resolve memo
    for name in (
        "rename", "replace", "unlink", "remove", "rmdir", "mkdir", "makedirs", "chmod",
        "symlink", "link", "utime", "chdir", "truncate", "removedirs", "renames",
    ):  # fmt: skip
        if hasattr(os, name):
            setattr(os, name, _bumping(getattr(os, name)))
    original_open = builtins.open

    def _open(file, mode="r", *args, **kwargs):
        if any(c in mode for c in "wax+"):
            _bump()
        return original_open(file, mode, *args, **kwargs)

    builtins.open = io.open = _open
    original_os_open = os.open

    def _os_open(path, flags, *args, **kwargs):
        if flags & _WRITE_FLAGS:
            _bump()
        return original_os_open(path, flags, *args, **kwargs)

    os.open = _os_open
    original_popen = subprocess.Popen.__init__

    def _popen(self, *args, **kwargs):
        _bump()
        return original_popen(self, *args, **kwargs)

    subprocess.Popen.__init__ = _popen
    original_wait = subprocess.Popen.wait

    def _wait(self, *args, **kwargs):
        try:
            return original_wait(self, *args, **kwargs)
        finally:
            _bump()

    subprocess.Popen.wait = _wait


if os.environ.get("APM_PATH_FAST", "1") != "0":
    install()
