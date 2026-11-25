"""Spawn-ahead worker oracle: pre-import the cold-install module set, signal READY,
then serve exactly ONE request (cwd + env on stdin) and exit. Single-use (W3d)."""
import importlib
import json
import os
import sys

mods = open(sys.argv[1]).read().split()
for n in mods:
    try:
        importlib.import_module(n)
    except BaseException:
        pass
sys.stdout.write("READY\n")
sys.stdout.flush()
req = json.loads(sys.stdin.readline())
t = os.times()
with open(req["stamp"], "w") as fh:
    fh.write(str(t.user + t.system))
code = 99
try:
    os.chdir(req["cwd"])
    os.environ.clear()
    os.environ.update(req["env"])
    fd = os.open(os.devnull, os.O_RDWR)
    for i in (0, 1, 2):
        os.dup2(fd, i)
    sys.argv = ["apm", "install"]
    from apm_cli.entrypoint import main

    try:
        main()
        code = 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    import atexit

    atexit._run_exitfuncs()
except BaseException:
    code = 98
os._exit(code)
