"""Fork-server oracle: pre-import the cold-install module set, fork one child per run."""
import importlib
import json
import os
import resource
import sys

mods = open(sys.argv[1]).read().split()
t0 = resource.getrusage(resource.RUSAGE_SELF)
ok = 0
for n in mods:
    try:
        importlib.import_module(n)
        ok += 1
    except BaseException:
        pass
t1 = resource.getrusage(resource.RUSAGE_SELF)
print(json.dumps({"preimport_cpu": round((t1.ru_utime + t1.ru_stime) - (t0.ru_utime + t0.ru_stime), 3),
                  "modules": len(mods), "imported": ok}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    pid = os.fork()
    if pid == 0:
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
    _, status, ru = os.wait4(pid, 0)
    print(json.dumps({"cpu": round(ru.ru_utime + ru.ru_stime, 3), "rc": os.waitstatus_to_exitcode(status)}), flush=True)
