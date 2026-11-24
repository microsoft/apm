"""Count subprocess spawns and imported modules of a measured ``apm`` child.

The journey harness puts this directory on ``PYTHONPATH`` when it sets
``APM_PERF_SPAWN_LOG``. An audit hook counts process spawns by executable
name; at exit each Python process appends one JSON line to the log. The cost
is one hook call per audited event and one write at exit.

The record also carries work counters for floor models: the process's own
CPU (``self_cpu_s``; children CPU = the harness total minus it), git spawns by
subcommand, file opens for write and read, directory and link operations, and
hash objects built (with the bytes passed at construction).
"""

import atexit
import json
import os
import sys

_LOG = os.environ.get("APM_PERF_SPAWN_LOG")

if _LOG:
    _counts = {}
    _git_sub = {}
    _git_callers = {}
    _ops = {}
    _WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    _FS_EVENTS = {
        "os.mkdir": "mkdir",
        "os.rename": "rename",
        "os.link": "link",
        "os.symlink": "symlink",
        "os.remove": "remove",
        "os.rmdir": "rmdir",
        "os.listdir": "listdir",
        "os.scandir": "scandir",
        "os.chmod": "chmod",
        "shutil.copyfile": "copyfile",
        "shutil.copytree": "copytree",
        "shutil.rmtree": "rmtree",
        "socket.connect": "socket_connect",
    }

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

    def _git_subcommand(event, args):
        """The git subcommand (first non-option word after ``git``), best effort."""
        if event != "subprocess.Popen":
            return "?"
        argv = args[1]
        if isinstance(argv, bytes):
            argv = argv.decode(errors="replace")
        words = argv.split() if isinstance(argv, str) else [str(w) for w in argv]
        rest = words[1:]
        i = 0
        while i < len(rest):
            word = rest[i]
            if word in ("-c", "-C", "--git-dir", "--work-tree", "--namespace"):
                i += 2
                continue
            if word.startswith("-"):
                i += 1
                continue
            return word.strip('"')
        return "?"

    def _bump(table, key, by=1):
        table[key] = table.get(key, 0) + by

    def _hook(event, args):
        if event == "open":
            mode, flags = args[1], args[2]
            if isinstance(mode, str):
                write = any(c in mode for c in "wax+")
            else:
                write = bool((flags or 0) & _WRITE_FLAGS)
            _bump(_ops, "open_write" if write else "open_read")
            return
        op = _FS_EVENTS.get(event)
        if op is not None:
            _bump(_ops, op)
            return
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
        if name == "git":
            sub = _git_subcommand(event, args)
            _bump(_git_sub, sub)
            frames = []
            frame = sys._getframe(1)
            while frame is not None and len(frames) < 4:
                filename = frame.f_code.co_filename.replace("\\", "/")
                if "apm_cli/" in filename and "/utils/git_env.py" not in filename:
                    frames.append(filename.split("apm_cli/", 1)[1] + ":" + frame.f_code.co_name)
                frame = frame.f_back
            _bump(_git_callers, sub + " <- " + " < ".join(frames))

    def _wrap_hash(module, attr):
        original = getattr(module, attr, None)
        if original is None:
            return

        first = 1 if attr == "new" else 0

        def counted(*args, **kwargs):
            _bump(_ops, "hash_objects")
            data = args[first] if len(args) > first else kwargs.get("data", b"")
            if isinstance(data, (bytes, bytearray, memoryview)):
                _bump(_ops, "hash_init_bytes", len(data))
            return original(*args, **kwargs)

        setattr(module, attr, counted)

    import hashlib

    for _attr in ("sha256", "sha1", "sha512", "md5", "new"):
        _wrap_hash(hashlib, _attr)

    def _flush():
        times = os.times()
        record = {
            "pid": os.getpid(),
            "spawns": _counts,
            "modules": len(sys.modules),
            "self_cpu_s": round(times.user + times.system, 4),
            "children_cpu_s": round(times.children_user + times.children_system, 4),
            "git_sub": _git_sub,
            "git_callers": _git_callers,
            "ops": _ops,
        }
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    sys.addaudithook(_hook)
    atexit.register(_flush)
