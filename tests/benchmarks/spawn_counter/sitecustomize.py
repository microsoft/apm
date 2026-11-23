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
    _git_subcommands = {}

    def _git_subcommand(argv):
        """The subcommand of a git argv list, past global options and their values."""
        items = iter(argv[1:])
        for item in items:
            item = item.decode(errors="replace") if isinstance(item, bytes) else str(item)
            if item in ("-c", "-C"):
                next(items, None)
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
        if name == "git" and event == "subprocess.Popen" and not command_line:
            sub = _git_subcommand(list(args[1]))
            _git_subcommands[sub] = _git_subcommands.get(sub, 0) + 1

    def _flush():
        record = {
            "pid": os.getpid(),
            "spawns": _counts,
            "git_subcommands": _git_subcommands,
            "modules": len(sys.modules),
        }
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    sys.addaudithook(_hook)
    atexit.register(_flush)
