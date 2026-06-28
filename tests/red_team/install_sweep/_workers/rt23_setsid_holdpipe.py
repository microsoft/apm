"""Round-23 worker: a grandchild that ``setsid``s OUT of the shell's process
group while still HOLDING the inherited capture pipe write-ends open.

Invoked as the *shell command* of a lifecycle script. It forks a grandchild,
the grandchild calls ``os.setsid()`` (the canonical daemon-detach idiom) so it
becomes the leader of a BRAND-NEW process group whose pgid == its own pid (and
therefore != the shell-leader pid that ``_capture_bounded`` reaps with
``os.killpg(proc.pid, SIGKILL)``), records its pid to argv[1], then keeps fds
1/2 (the capture pipes) OPEN and sleeps. The shell-level parent exits 0.

Because the grandchild left the group, the round-22 surgical reap
(``killpg(proc.pid)``) cannot reach it: the drains stay wedged on ``read()``,
the final ``join(timeout=5)`` burns the full 5s, and the grandchild + its two
drain daemons LEAK past the return of ``_capture_bounded``.

Usage:
    python rt23_setsid_holdpipe.py <pidfile> <sleep_seconds>
"""

from __future__ import annotations

import os
import sys
import time

pidfile = sys.argv[1]
sleep_s = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

pid = os.fork()
if pid > 0:
    # Shell's direct child: exit 0 at once so proc.wait() sees "done".
    os._exit(0)

# Grandchild: detach into a new session/process group. fds 1/2 (the capture
# pipe write-ends) are PRESERVED across setsid -- that is the wedge.
os.setsid()
with open(pidfile, "w") as fh:
    fh.write(str(os.getpid()))
    fh.flush()
# Hold the inherited capture pipes open while we sleep.
time.sleep(sleep_s)
os._exit(0)
