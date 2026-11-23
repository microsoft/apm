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

    def _target(event, args):
        if event == "subprocess.Popen":
            executable, argv = args[0], args[1]
            if executable is None:
                executable = argv if isinstance(argv, (str, bytes)) else (list(argv) or ["?"])[0]
            return executable
        if event in ("os.system", "os.exec"):
            return args[0]
        if event == "os.spawn":
            return args[1]
        return None

    def _hook(event, args):
        if event not in ("subprocess.Popen", "os.system", "os.exec", "os.spawn"):
            return
        target = _target(event, args)
        if isinstance(target, bytes):
            target = target.decode(errors="replace")
        name = os.path.basename(str(target).split()[0]) if target else "?"
        _counts[name] = _counts.get(name, 0) + 1

    def _flush():
        record = {"pid": os.getpid(), "spawns": _counts, "modules": len(sys.modules)}
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    sys.addaudithook(_hook)
    atexit.register(_flush)
