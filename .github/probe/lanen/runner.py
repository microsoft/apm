"""Runs `apm install` in this process in one of several research modes (apm venv python)."""
import atexit
import sys

mode = sys.argv[1]
if mode == "imports":
    import importlib

    for name in open(sys.argv[2]).read().split():
        try:
            importlib.import_module(name)
        except BaseException:
            pass
    sys.exit(0)
if mode == "dump":
    out = sys.argv[2]

    def _dump():
        names = sorted(k for k, v in list(sys.modules.items()) if v is not None)
        with open(out, "w") as fh:
            fh.write("\n".join(n for n in names if n not in ("__main__", "counter")))

    atexit.register(_dump)
if mode == "count":
    import counter

    counter.install(sys.argv[2])
sys.argv = ["apm", "install"]
from apm_cli.entrypoint import main  # noqa: E402

main()
