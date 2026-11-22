import os
import sys

_LOG = os.environ.get("L5_ARGV_LOG")
if _LOG:

    def _hook(event, args):
        if event == "subprocess.Popen":
            with open(_LOG, "a", encoding="utf-8") as handle:
                handle.write(repr(args[1]) + "\n")

    sys.addaudithook(_hook)
