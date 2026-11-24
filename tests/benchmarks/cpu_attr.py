"""Per-thread CPU sampler and per-child CPU for one ``apm`` run (bench harness only).

Usage: ``python cpu_attr.py -o OUT.json (SCRIPT | --entry module:func) [args...]``

Python CPU: a sampling thread charges each thread's own CPU delta since the
last sample (``pthread_getcpuclockid`` / ``GetThreadTimes``) to that thread's
current stack, so idle waits cost nothing and worker threads are counted.
Git children: POSIX reaps through ``os.wait4`` (exact CPU of the child and its
waited-for descendants, per git subcommand). Windows puts this process in a
Job Object at start, so every descendant is in the job total; per-subcommand
CPU there is the direct child's own time (a launcher can hide work: lower bound).
"""

from __future__ import annotations

import collections
import json
import os
import runpy
import subprocess
import sys
import threading
import time

_INTERVAL = 0.005
_START_CPU = time.process_time()
_WIN = sys.platform == "win32"


def _subcommand(args) -> str:
    try:
        argv = [str(a) for a in (args if isinstance(args, (list, tuple)) else [args])]
    except Exception:
        return "?"
    prog = os.path.basename(argv[0]).lower() if argv else "?"
    if not prog.startswith("git"):
        return prog
    skip_value = {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}
    rest = iter(argv[1:])
    for item in rest:
        if item in skip_value:
            next(rest, None)
        elif not item.startswith("-"):
            if item == "config":
                flags = [a for a in argv[argv.index(item) + 1 :] if a.startswith("--")]
                return "git config " + " ".join(flags[:3])
            return f"git {item}"
    return "git"


_children: dict[str, list[float]] = collections.defaultdict(lambda: [0, 0.0])
_children_lock = threading.Lock()


_children_by_caller: dict[str, list[float]] = collections.defaultdict(lambda: [0, 0.0])


def _note_child(popen, cpu: float) -> None:
    if getattr(popen, "_attr_noted", False):
        return
    popen._attr_noted = True
    sub = _subcommand(popen.args)
    with _children_lock:
        slot = _children[sub]
        slot[0] += 1
        slot[1] += cpu
        slot = _children_by_caller[f"{sub} <- {getattr(popen, '_attr_caller', '?')}"]
        slot[0] += 1
        slot[1] += cpu


_orig_popen_init = subprocess.Popen.__init__


def _popen_init(self, *args, **kwargs):
    frame, owner, depth = sys._getframe(1), "<no apm frame>", 0
    while frame is not None and depth < 80:
        path = frame.f_code.co_filename.replace("\\", "/")
        marker = path.rfind("apm_cli/")
        if marker >= 0:
            owner = f"{path[marker:]}:{frame.f_code.co_name}"
            break
        frame, depth = frame.f_back, depth + 1
    self._attr_caller = owner
    _orig_popen_init(self, *args, **kwargs)


subprocess.Popen.__init__ = _popen_init


if _WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenThread.restype = wintypes.HANDLE
    _k32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    # Without argtypes ctypes passes a Python int as a C int: the pseudo-handle
    # of GetCurrentProcess (-1 as an unsigned pointer) overflows it.
    _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _LPFT = ctypes.POINTER(wintypes.FILETIME)
    _k32.GetThreadTimes.argtypes = (wintypes.HANDLE, _LPFT, _LPFT, _LPFT, _LPFT)
    _k32.GetProcessTimes.argtypes = (wintypes.HANDLE, _LPFT, _LPFT, _LPFT, _LPFT)
    _k32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    )

    def _ft(ft) -> float:
        return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

    def _times(fn, handle) -> float | None:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not fn(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        return _ft(k) + _ft(u)

    _thread_handles: dict[int, int] = {}

    def _thread_cpu(thread: threading.Thread) -> float | None:
        native = thread.native_id
        handle = _thread_handles.get(native)
        if handle is None:
            handle = _k32.OpenThread(0x0800, False, native)
            if not handle:
                return None
            _thread_handles[native] = handle
        return _times(_k32.GetThreadTimes, handle)

    class _Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    _JOB = _k32.CreateJobObjectW(None, None)
    _JOB_OK = bool(_JOB) and bool(_k32.AssignProcessToJobObject(_JOB, _k32.GetCurrentProcess()))

    def _job_cpu() -> tuple[float, int] | None:
        if not _JOB_OK:
            return None
        info = _Accounting()
        if not _k32.QueryInformationJobObject(
            _JOB, 1, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            return None
        return (info.TotalUserTime + info.TotalKernelTime) / 1e7, info.TotalProcesses

    def _child_cpu(popen) -> float | None:
        return _times(_k32.GetProcessTimes, int(popen._handle))

    for _name in ("_wait", "_internal_poll"):
        _orig = getattr(subprocess.Popen, _name)

        def _patched(self, *a, __orig=_orig, **k):
            result = __orig(self, *a, **k)
            if self.returncode is not None and not getattr(self, "_attr_noted", False):
                cpu = _child_cpu(self)
                if cpu is not None:
                    _note_child(self, cpu)
            return result

        setattr(subprocess.Popen, _name, _patched)
else:

    def _thread_cpu(thread: threading.Thread) -> float | None:
        try:
            return time.clock_gettime(time.pthread_getcpuclockid(thread.ident))
        except Exception:
            return None

    def _job_cpu():
        return None

    def _wait4(popen, pid, flags):
        found, status, usage = os.wait4(pid, flags)
        if found == popen.pid:
            _note_child(popen, usage.ru_utime + usage.ru_stime)
        return found, status

    def _try_wait(self, wait_flags):
        try:
            return _wait4(self, self.pid, wait_flags)
        except ChildProcessError:
            return self.pid, 0

    _orig_poll = subprocess.Popen._internal_poll

    def _internal_poll(self, _deadstate=None, **kwargs):
        kwargs["_waitpid"] = lambda pid, flags: _wait4(self, pid, flags)
        return _orig_poll(self, _deadstate=_deadstate, **kwargs)

    subprocess.Popen._try_wait = _try_wait
    subprocess.Popen._internal_poll = _internal_poll


def _where(code) -> str:
    path = code.co_filename.replace("\\", "/")
    marker = path.rfind("apm_cli/")
    if marker >= 0:
        path = path[marker:]
    elif "importlib/_bootstrap" in path or path.startswith("<frozen importlib"):
        path = "<import>"
    else:
        path = (
            path.rsplit("/", 2)[-1]
            if "site-packages" not in path
            else path.split("site-packages/")[-1]
        )
    return path


_self = collections.Counter()
_incl = collections.Counter()
_module_incl = collections.Counter()
_imports = [0.0]
_samples = [0]
# Check attribution: CPU by innermost apm_cli function (stdlib below it folded in),
# and the stdlib leaf kind under that apm function (e.g. realpath <- path_security:f).
_apm_owner = collections.Counter()
_leaf_owner = collections.Counter()
_realpath_calls = collections.Counter()
_ops = collections.Counter()
_op_totals = collections.Counter()
_stop = threading.Event()


def _sample() -> None:
    me = threading.get_ident()
    previous: dict[int, float] = {}
    while not _stop.wait(_INTERVAL):
        threads = {t.ident: t for t in threading.enumerate()}
        for ident, frame in sys._current_frames().items():
            thread = threads.get(ident)
            if ident == me or thread is None:
                continue
            now = _thread_cpu(thread)
            if now is None:
                continue
            delta = now - previous.get(ident, now)
            previous[ident] = now
            if delta <= 0:
                continue
            _samples[0] += 1
            keys, modules, importing = [], set(), False
            depth = 0
            while frame is not None and depth < 200:
                where = _where(frame.f_code)
                if where == "<import>":
                    importing = True
                keys.append(f"{where}:{frame.f_code.co_name}")
                if where.startswith("apm_cli/"):
                    modules.add(where)
                frame, depth = frame.f_back, depth + 1
            if importing:
                _imports[0] += delta
            if keys:
                _self[keys[0]] += delta
                owner = next((k for k in keys if k.startswith("apm_cli/")), "<no apm frame>")
                _apm_owner[owner] += delta
                if not keys[0].startswith("apm_cli/"):
                    _leaf_owner[f"{keys[0]} <- {owner}"] += delta
            for key in set(keys):
                _incl[key] += delta
            for module in modules:
                _module_incl[module] += delta


def _report(out: str, wall: float, exit_code) -> None:
    times = os.times()
    process_cpu = times.user + times.system
    children_total = None
    job = _job_cpu()
    if job is not None:
        children_total = round(job[0] - process_cpu, 4)
    elif not _WIN:
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        children_total = round(usage.ru_utime + usage.ru_stime, 4)
    top = lambda counter, n: [[k, round(v, 4)] for k, v in counter.most_common(n)]  # noqa: E731
    data = {
        "wall_s": round(wall, 4),
        "exit": exit_code,
        "process_cpu_s": round(process_cpu, 4),
        "startup_cpu_s": round(_START_CPU, 4),
        "import_cpu_s": round(_imports[0], 4),
        "sampled_cpu_s": round(sum(_self.values()), 4),
        "samples": _samples[0],
        "children_cpu_total_s": children_total,
        "children_by_subcommand": {
            k: {"count": v[0], "cpu_s": round(v[1], 4)}
            for k, v in sorted(_children.items(), key=lambda kv: -kv[1][1])
        },
        "children_by_subcommand_caller": {
            k: {"count": v[0], "cpu_s": round(v[1], 4)}
            for k, v in sorted(_children_by_caller.items(), key=lambda kv: -kv[1][1])
        },
        "children_exact": not _WIN,
        "module_inclusive": top(_module_incl, 40),
        "function_inclusive": top(_incl, 60),
        "function_self": top(_self, 60),
        "apm_owner_self": top(_apm_owner, 150),
        "leaf_by_apm_owner": top(_leaf_owner, 150),
        "realpath_calls_by_apm_owner": top(_realpath_calls, 60),
        "realpath_calls_total": sum(_realpath_calls.values()),
        "op_totals": dict(_op_totals.most_common()),
        "ops_by_apm_owner": top(_ops, 300),
    }
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)


def _count_realpath() -> None:
    """Count os.path.realpath calls (Path.resolve uses it) by innermost apm_cli caller."""
    original = os.path.realpath

    def counted(path, *args, **kwargs):
        frame, owner, depth = sys._getframe(1), "<no apm frame>", 0
        while frame is not None and depth < 60:
            where = _where(frame.f_code)
            if where.startswith("apm_cli/"):
                owner = f"{where}:{frame.f_code.co_name}"
                break
            frame, depth = frame.f_back, depth + 1
        _realpath_calls[owner] += 1
        return original(path, *args, **kwargs)

    os.path.realpath = counted
    import pathlib

    if hasattr(pathlib, "os") and getattr(pathlib.os.path, "realpath", None) is not counted:
        pathlib.os.path.realpath = counted


def _owner(start) -> str:
    frame, depth = start, 0
    while frame is not None and depth < 60:
        where = _where(frame.f_code)
        if where.startswith("apm_cli/"):
            return f"{where}:{frame.f_code.co_name}"
        frame, depth = frame.f_back, depth + 1
    return "<no apm frame>"


def _count_ops() -> None:
    """Count file-system and hash operations by innermost apm_cli caller (counts, not CPU)."""
    import builtins
    import hashlib

    def wrap(module, attr, op):
        original = getattr(module, attr, None)
        if original is None:
            return

        def counted(*args, **kwargs):
            _op_totals[op] += 1
            _ops[f"{op} <- {_owner(sys._getframe(1))}"] += 1
            return original(*args, **kwargs)

        setattr(module, attr, counted)

    for attr in ("stat", "lstat", "scandir", "listdir", "open", "mkdir", "rename", "replace", "unlink"):
        wrap(os, attr, f"os.{attr}")
    wrap(builtins, "open", "open")
    for attr in ("sha256", "sha1", "new"):
        wrap(hashlib, attr, f"hashlib.{attr}")


def main() -> int:
    _count_realpath()
    _count_ops()
    args = sys.argv[1:]
    out = args[args.index("-o") + 1]
    del args[args.index("-o") : args.index("-o") + 2]
    entry = None
    if args[0] == "--entry":
        entry, args = args[1], args[2:]
        sys.argv = ["apm", *args]
    else:
        sys.argv = list(args)
    sampler = threading.Thread(target=_sample, name="cpu-attr", daemon=True)
    started = time.perf_counter()
    sampler.start()
    code = 0
    try:
        if entry:
            module, func = entry.split(":")
            mod = __import__(module, fromlist=[func])
            code = getattr(mod, func)()
        else:
            runpy.run_path(sys.argv[0], run_name="__main__")
    except SystemExit as exc:
        code = exc.code
    finally:
        _stop.set()
        sampler.join(1)
        _report(out, time.perf_counter() - started, code)
    return code if isinstance(code, int) else (0 if code is None else 1)


if __name__ == "__main__":
    sys.exit(main())
