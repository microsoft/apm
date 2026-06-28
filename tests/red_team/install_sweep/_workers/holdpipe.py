"""Round-22 worker: leak a grandchild that holds the capture pipe open.

Invoked as the *shell command* of a lifecycle script. It forks a detached
grandchild that INHERITS the parent's stdout/stderr (the capture pipes),
writes that grandchild's pid to argv[1], then the shell-level parent exits
0 immediately. The grandchild keeps the pipe write-ends open while it sleeps,
so the installer's drain threads never see EOF.

Usage (as a command-script run string):
    python holdpipe.py <pidfile> <sleep_seconds>
"""

from __future__ import annotations

import os
import sys
import time

pidfile = sys.argv[1]
sleep_s = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

# First fork: detach grandchild from the python parent.
pid = os.fork()
if pid > 0:
    # Parent (the shell's direct child) records the grandchild pid and exits 0
    # immediately -- the shell leader is then "done" from proc.wait()'s view.
    with open(pidfile, "w") as fh:
        fh.write(str(pid))
    os._exit(0)

# Grandchild: keep stdout/stderr (the inherited capture pipes) OPEN and sleep.
# We deliberately do NOT close fds 1/2 -- that is the whole point: the write
# end of each pipe stays open so the installer's _drain_capped never reads EOF.
time.sleep(sleep_s)
os._exit(0)
