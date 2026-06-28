"""Round-30 worker: an ASYMMETRIC pipe-holder grandchild.

Invoked as the shell command of a lifecycle script. It forks a grandchild that
CLOSES stdout (fd 1) but KEEPS stderr (fd 2 -- the inherited capture pipe
write-end) OPEN, then records its pid and sleeps. The shell-level parent exits
0 at once.

This stresses the round-22/23 surgical reap from one side only: the stdout
drain hits EOF immediately (well-behaved), while the stderr drain stays wedged
on ``read()``. The grandchild stays in the shell's process group (no setsid),
so ``killpg(proc.pid)`` MUST reach it and reap the group; otherwise the stderr
drain leaks and/or the install hangs on the join budget.

Usage:
    python rt30_asym_drain.py <pidfile> <sleep_seconds>
"""

from __future__ import annotations

import os
import sys
import time

pidfile = sys.argv[1]
sleep_s = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

pid = os.fork()
if pid > 0:
    # Shell leader's direct child: exit 0 so proc.wait() sees "done".
    os._exit(0)

# Grandchild: drop stdout (fd 1) so that drain EOFs, but HOLD stderr (fd 2).
os.close(1)
with open(pidfile, "w") as fh:
    fh.write(str(os.getpid()))
    fh.flush()
time.sleep(sleep_s)
os._exit(0)
