"""Count subprocess spawns and imported modules of a measured ``apm`` child.

The journey harness puts this directory on ``PYTHONPATH`` when it sets
``APM_PERF_SPAWN_LOG``. An audit hook counts process spawns by executable
name and git spawns by verb; at exit each Python process appends one JSON
line to the log. The cost is one hook call per audited event and one write
at exit.

``APM_PERF_COUNT_FS=1`` (the counters fixture, never a timed run) also counts
files opened for writing, lockfile reads, ``realpath``/``stat`` calls and
hashing (objects and bytes). Those wrappers cost CPU, so timings taken with
them are not comparable.
"""

import atexit
import json
import os
import sys

_LOG = os.environ.get("APM_PERF_SPAWN_LOG")

if _LOG:
    _counts = {}
    _git_verbs = {}
    _fs = {}
    _GIT_VALUE_FLAGS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}

    def _bump(table, key, by=1):
        table[key] = table.get(key, 0) + by

    def _git_verb(argv):
        rest = iter(argv[1:])
        for item in rest:
            item = item.decode(errors="replace") if isinstance(item, bytes) else str(item)
            if item in _GIT_VALUE_FLAGS:
                next(rest, None)
            elif not item.startswith("-"):
                return item
        return "?"

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
        if name.lower() == "git" and event == "subprocess.Popen":
            argv = args[1]
            if isinstance(argv, (list, tuple)):
                _bump(_git_verbs, _git_verb(list(argv)))
            elif isinstance(argv, (str, bytes)):
                text = argv.decode(errors="replace") if isinstance(argv, bytes) else argv
                _bump(_git_verbs, _git_verb(text.split()))

    def _flush():
        record = {"pid": os.getpid(), "spawns": _counts, "modules": len(sys.modules)}
        record["git_verbs"] = _git_verbs
        if _fs:
            record["fs"] = _fs
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    sys.addaudithook(_hook)
    atexit.register(_flush)

    if os.environ.get("APM_PERF_COUNT_FS") == "1":
        import hashlib

        _WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

        def _fs_hook(event, args):
            if event != "open":
                return
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & _WRITE_FLAGS
            )
            if writing:
                _bump(_fs, "files_written")
            elif os.path.basename(str(path)) == "apm.lock.yaml":
                _bump(_fs, "lockfile_reads")

        sys.addaudithook(_fs_hook)

        def _counted(module, attr, key):
            real = getattr(module, attr)

            def wrapper(*a, **k):
                _bump(_fs, key)
                return real(*a, **k)

            setattr(module, attr, wrapper)

        _counted(os.path, "realpath", "realpath_calls")
        _counted(os, "stat", "stat_calls")
        _counted(os, "lstat", "lstat_calls")

        class _Hash:
            __slots__ = ("_h",)

            def __init__(self, h):
                self._h = h

            def update(self, data):
                _bump(_fs, "hashed_bytes", len(data))
                self._h.update(data)

            def copy(self):
                return _Hash(self._h.copy())

            def __getattr__(self, name):
                return getattr(self._h, name)

        def _hash_factory(real):
            def make(*a, **k):
                _bump(_fs, "hash_objects")
                data = a[0] if a else k.get("data", k.get("string", b""))
                if data:
                    _bump(_fs, "hashed_bytes", len(data))
                return _Hash(real(*a, **k))

            return make

        for _name in ("sha256", "sha1", "sha512", "md5"):
            setattr(hashlib, _name, _hash_factory(getattr(hashlib, _name)))

        _real_new = hashlib.new

        def _new(name, *a, **k):
            # hashlib.new(name, data): git object ids; hashlib.file_digest(f, "sha256")
            # also builds its digest through the module's ``new``, then feeds it chunks.
            _bump(_fs, "hash_objects")
            data = a[0] if a else k.get("data", k.get("string", b""))
            if data:
                _bump(_fs, "hashed_bytes", len(data))
            return _Hash(_real_new(name, *a, **k))

        hashlib.new = _new
