"""Count subprocess spawns and imported modules of a measured ``apm`` child.

The journey harness puts this directory on ``PYTHONPATH`` when it sets
``APM_PERF_SPAWN_LOG``. An audit hook counts process spawns by executable
name; at exit each Python process appends one JSON line to the log. The cost
is one hook call per audited event and one write at exit.
"""

import atexit
import json
import os
import sys

_LOG = os.environ.get("APM_PERF_SPAWN_LOG")

if _LOG:
    _counts = {}

    def _program(command_line):
        """The first program of a command line; Windows quotes a path with spaces."""
        text = command_line.lstrip()
        if text.startswith('"'):
            return text[1:].split('"', 1)[0]
        return (text.split() or [""])[0]

    def _target(event, args):
        """``(target, is_command_line)``: a command line is parsed, a path is not.

        On Windows ``subprocess.Popen`` audits the joined command line.
        """
        if event == "subprocess.Popen":
            executable, argv = args[0], args[1]
            if executable is None:
                if isinstance(argv, (str, bytes)):
                    return argv, True
                return (list(argv) or ["?"])[0], False
            return executable, False
        if event == "os.system":
            return args[0], True
        if event == "os.exec":
            return args[0], False
        if event == "os.spawn":
            return args[1], False
        return None, False

    def _hook(event, args):
        if event not in ("subprocess.Popen", "os.system", "os.exec", "os.spawn"):
            return
        target, command_line = _target(event, args)
        if isinstance(target, bytes):
            target = target.decode(errors="replace")
        target = str(target) if target else ""
        if command_line:
            target = _program(target)
        name = os.path.basename(target) or "?"
        # ``git.exe`` on Windows counts as ``git``, so counts compare across OSes.
        if os.name == "nt" and name.lower().endswith(".exe"):
            name = name[:-4]
        _counts[name] = _counts.get(name, 0) + 1

    # Research (cold-install spawn attribution): per spawn class, the count,
    # the child's own user+sys CPU (wait4 rusage, which includes descendants
    # the child reaped), the parent thread CPU inside Popen.__init__, and wall.
    import inspect
    import subprocess
    import threading
    import time

    _attr = {}
    _pid_key = {}
    _pid_start = {}
    _lock = threading.Lock()
    _VALUED = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}
    _CONFIG_MODES = (
        "--get-urlmatch",
        "--get-regexp",
        "--get-all",
        "--get",
        "--list",
        "--unset",
        "--add",
    )

    def _klass(args):
        argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        argv = [a.decode(errors="replace") if isinstance(a, bytes) else str(a) for a in argv]
        if len(argv) == 1:
            argv = argv[0].split()
        name = os.path.basename(argv[0]) if argv else "?"
        if name.lower().endswith(".exe"):
            name = name[:-4]
        if name != "git":
            return name
        rest = argv[1:]
        i = 0
        while i < len(rest):
            token = rest[i]
            if token in _VALUED:
                i += 2
                continue
            if token.startswith("-"):
                i += 1
                continue
            sub = token
            if sub == "config":
                mode = next((m for m in _CONFIG_MODES if m in rest[i + 1 :]), "set")
                sub = f"config {mode}"
            elif sub in ("remote", "worktree", "sparse-checkout", "submodule", "lfs"):
                nxt = next((t for t in rest[i + 1 :] if not t.startswith("-")), "")
                sub = f"{sub} {nxt}".strip()
            return f"git {sub}"
        return "git"

    def _add(key, *, count=0, child=0.0, parent=0.0, wall=0.0):
        with _lock:
            row = _attr.setdefault(key, [0, 0.0, 0.0, 0.0])
            row[0] += count
            row[1] += child
            row[2] += parent
            row[3] += wall

    def _reaped(pid, ru):
        key = _pid_key.pop(pid, None)
        if key is None:
            return
        start = _pid_start.pop(pid, None)
        wall = time.perf_counter() - start if start is not None else 0.0
        _add(key, child=ru.ru_utime + ru.ru_stime, wall=wall)

    def _waitpid(pid, flags):
        got, status, ru = os.wait4(pid, flags)
        if got:
            _reaped(got, ru)
        return got, status

    _init = subprocess.Popen.__init__

    def _popen_init(self, args, *a, **kw):
        key = _klass(args)
        before = time.thread_time()
        _init(self, args, *a, **kw)
        _add(key, count=1, parent=time.thread_time() - before)
        pid = getattr(self, "pid", None)
        if pid:
            _pid_key[pid] = key
            _pid_start[pid] = time.perf_counter()

    if os.name != "nt" and hasattr(os, "wait4"):
        subprocess.Popen.__init__ = _popen_init

        def _try_wait(self, wait_flags):
            try:
                return _waitpid(self.pid, wait_flags)
            except ChildProcessError:
                return self.pid, 0

        subprocess.Popen._try_wait = _try_wait
        _poll = subprocess.Popen._internal_poll
        _poll_params = inspect.signature(_poll).parameters
        if "_waitpid" in _poll_params:

            def _internal_poll(self, _deadstate=None, **kw):
                kw["_waitpid"] = _waitpid
                return _poll(self, _deadstate, **kw)

            subprocess.Popen._internal_poll = _internal_poll
        elif "_del_safe" in _poll_params:
            _safe = _poll_params["_del_safe"].default

            class _DelSafe:
                def __getattr__(self, name):
                    return _waitpid if name == "waitpid" else getattr(_safe, name)

            _del_safe = _DelSafe()

            def _internal_poll(self, _deadstate=None, **kw):
                kw["_del_safe"] = _del_safe
                return _poll(self, _deadstate, **kw)

            subprocess.Popen._internal_poll = _internal_poll

    def _flush():
        import resource

        own = resource.getrusage(resource.RUSAGE_SELF)
        kids = resource.getrusage(resource.RUSAGE_CHILDREN)
        record = {"pid": os.getpid(), "spawns": _counts, "modules": len(sys.modules)}
        record["attr"] = {
            k: [v[0], round(v[1], 4), round(v[2], 4), round(v[3], 4)] for k, v in _attr.items()
        }
        record["rusage"] = {
            "self": round(own.ru_utime + own.ru_stime, 4),
            "children": round(kids.ru_utime + kids.ru_stime, 4),
            "unreaped": len(_pid_key),
        }
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    sys.addaudithook(_hook)
    atexit.register(_flush)
