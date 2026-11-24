"""RESEARCH ONLY (no PR): wall, process-CPU and thread-CPU spans for perf CI runs."""

import atexit
import functools
import json
import os
import sys
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_acc: dict[str, list[float]] = {}
_t0 = time.perf_counter()
_phase = ["pre"]
_spawns: dict[str, list[float]] = {}
_SKIP_NEXT = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
_DETAIL = {"cat-file", "config", "ls-tree", "rev-parse", "show-ref", "ls-remote", "fetch", "for-each-ref"}


def _subcommand(argv) -> str:
    try:
        if isinstance(argv, (str, bytes)):
            argv = str(argv).split()
        argv = [str(a) for a in argv]
        prog = os.path.basename(argv[0]).lower().removesuffix(".exe")
        rest = argv[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok in _SKIP_NEXT:
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            sub = tok
            if sub in _DETAIL:
                opts = [t for t in rest[i + 1 : i + 4] if t.startswith("-")][:2]
                sub = " ".join([sub, *opts])
            return f"{prog} {sub}"
        return prog
    except Exception:
        return "?"


def _child_cpu(proc) -> float:
    if os.name != "nt":
        return 0.0
    try:
        import ctypes
        from ctypes import wintypes

        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        ok = ctypes.windll.kernel32.GetProcessTimes(
            int(proc._handle), ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)
        )
        if not ok:
            return -1.0

        def f(t):
            return ((t.dwHighDateTime << 32) | t.dwLowDateTime) / 1e7

        return f(k) + f(u)
    except Exception:
        return -1.0


def _install_spawn_hooks() -> None:
    import subprocess

    orig_init = subprocess.Popen.__init__
    orig_wait = subprocess.Popen._wait

    def init(self, args, *a, **kw):
        self._rs_key = f"{_phase[0]}|{_subcommand(args)}"
        self._rs_t = time.perf_counter()
        self._rs_done = False
        return orig_init(self, args, *a, **kw)

    def wait(self, *a, **kw):
        rc = orig_wait(self, *a, **kw)
        if rc is not None and not getattr(self, "_rs_done", True):
            self._rs_done = True
            cpu = _child_cpu(self)
            with _lock:
                v = _spawns.setdefault(self._rs_key, [0, 0.0, 0.0])
                v[0] += 1
                v[1] += time.perf_counter() - self._rs_t
                v[2] += cpu
        return rc

    subprocess.Popen.__init__ = init
    subprocess.Popen._wait = wait


_install_spawn_hooks()


def _add(name: str, wall: float, pcpu: float, tcpu: float) -> None:
    with _lock:
        a = _acc.setdefault(name, [0, 0.0, 0.0, 0.0])
        a[0] += 1
        a[1] += wall
        a[2] += pcpu
        a[3] += tcpu


def wrap(name: str, fn):
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        w, p, t = time.perf_counter(), time.process_time(), time.thread_time()
        prev = _phase[0]
        if name.startswith("phase."):
            _phase[0] = name[6:]
        try:
            return fn(*args, **kwargs)
        finally:
            _phase[0] = prev
            _add(
                name,
                time.perf_counter() - w,
                time.process_time() - p,
                time.thread_time() - t,
            )

    return inner


def patch(cls, attr: str, name: str) -> None:
    setattr(cls, attr, wrap(name, getattr(cls, attr)))


def _dump() -> None:
    try:
        if "install" not in sys.argv:
            return
        ws = os.environ.get("GITHUB_WORKSPACE")
        root = Path(ws) if ws else Path(sys.prefix).parent.parent
        outs = sorted((root / "out").glob("shard-*/runs"))
        if not outs:
            return
        rec = {
            "argv": sys.argv[1:],
            "pid": os.getpid(),
            "wall_since_import": time.perf_counter() - _t0,
            "process_cpu_total": time.process_time(),
            "spawns": {k: {"n": v[0], "wall": v[1], "cpu": v[2]} for k, v in _spawns.items()},
            "spans": {
                k: {"n": v[0], "wall": v[1], "pcpu": v[2], "tcpu": v[3]} for k, v in _acc.items()
            },
        }
        name = f"spans-{os.getpid()}-{time.time_ns()}.json"
        (outs[0] / name).write_text(json.dumps(rec), encoding="utf-8")
    except Exception:
        pass


atexit.register(_dump)
