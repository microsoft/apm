"""Research oracle (never merge): run ``apm <argv>`` in-process with every module a
real run imports already imported, and time only the run (PERF VP 07:23, W t10).

Usage: python preimport_runner.py <modules-file> <result-file> <argv...>
If <modules-file> does not exist, run once WITHOUT timing and write the modules
that run imported (sorted), then exit: the caller's next call is the timed one.
"""

import contextlib
import json
import sys
import time


def _run(argv):
    from apm_cli.entrypoint import main

    sys.argv = ["apm", *argv]
    try:
        code = main()
    except SystemExit as exc:
        code = exc.code
    return 0 if code is None else code


def main():
    modules_file, result_file, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
    try:
        with open(modules_file, encoding="utf-8") as handle:
            names = json.load(handle)
    except FileNotFoundError:
        code = _run(argv)
        with open(modules_file, "w", encoding="utf-8") as handle:
            json.dump(sorted(sys.modules), handle)
        return code
    import importlib

    for name in names:
        if name in sys.modules or name == "__main__":
            continue
        with contextlib.suppress(BaseException):  # an oracle tolerates unimportable names
            importlib.import_module(name)
    cpu0, wall0 = time.process_time(), time.perf_counter()
    code = _run(argv)
    cpu, wall = time.process_time() - cpu0, time.perf_counter() - wall0
    with open(result_file, "w", encoding="utf-8") as handle:
        json.dump({"cpu_s": cpu, "wall_s": wall, "code": code, "preloaded": len(names)}, handle)
    return code


if __name__ == "__main__":
    sys.exit(main())
