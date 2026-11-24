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
        try:
            return fn(*args, **kwargs)
        finally:
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
            "spans": {
                k: {"n": v[0], "wall": v[1], "pcpu": v[2], "tcpu": v[3]} for k, v in _acc.items()
            },
        }
        name = f"spans-{os.getpid()}-{time.time_ns()}.json"
        (outs[0] / name).write_text(json.dumps(rec), encoding="utf-8")
    except Exception:
        pass


atexit.register(_dump)
