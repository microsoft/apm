"""Run the apm CLI in this interpreter; L5_PROFILE=<path> profiles the whole run, imports included."""

import os
import sys

sys.argv = ["apm", *sys.argv[1:]]
target = os.environ.pop("L5_PROFILE", None)


def run():
    from apm_cli.entrypoint import main

    return main()


if target is None:
    sys.exit(run())
import cProfile  # noqa: E402

profiler = cProfile.Profile()
code = 0
try:
    profiler.runcall(run)
except SystemExit as exc:
    code = exc.code if isinstance(exc.code, int) else 0
finally:
    profiler.dump_stats(target)
sys.exit(code)
