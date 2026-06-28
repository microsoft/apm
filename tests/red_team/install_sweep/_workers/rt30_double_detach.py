"""Round-30 worker: a grandchild that DOUBLE-DETACHES (re-forks + setsid) only
AFTER a short delay, to escape the process group in the window between the
shell leader's exit and the installer's ``killpg``.

Sequence:
  * The shell leader's direct child forks a grandchild and exits 0 at once
    (so ``proc.wait()`` returns immediately).
  * The grandchild FIRST stays in the original group and HOLDS the capture
    pipes (fds 1/2) open, so the drains are wedged and the installer is forced
    into its surgical-reap path.
  * After ``escape_delay`` seconds the grandchild calls ``os.setsid()`` to form
    its OWN session/group and re-forks once more, then keeps holding the pipes.

If the installer's reap is bounded and fires while the grandchild is still in
the group, the group reap reaches it. If the grandchild escapes first, the
installer must still RETURN promptly (bounded latency) -- never hang. Either
way the install MUST be bounded.

Usage:
    python rt30_double_detach.py <pidfile> <escape_delay> <sleep_seconds>
"""

from __future__ import annotations

import os
import sys
import time

pidfile = sys.argv[1]
escape_delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.25
sleep_s = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0

pid = os.fork()
if pid > 0:
    os._exit(0)

# Grandchild stays in-group, holding fds 1/2, for escape_delay seconds.
time.sleep(escape_delay)
# Now escape: new session/group + a fresh re-fork to fully detach.
os.setsid()
pid2 = os.fork()
if pid2 > 0:
    os._exit(0)
with open(pidfile, "w") as fh:
    fh.write(str(os.getpid()))
    fh.flush()
time.sleep(sleep_s)
os._exit(0)
