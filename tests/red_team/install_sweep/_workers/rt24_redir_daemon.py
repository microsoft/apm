"""Round-24 worker: a legitimately-detached daemon that REDIRECTS its stdout
and stderr away from the capture pipes (npm/yarn parity -- MUST survive an
``apm install``) but inherits stdin and never reads it.

This mirrors the maintainer-protected case ``nohup svc >svc.log 2>&1 &``:

  - fd 1 / fd 2 (the capture pipe write-ends) are dup2'd onto a log FILE, so
    the parent's stdout/stderr drains hit EOF immediately -- the round-22
    contract says such a daemon is NEVER reaped.
  - fd 0 (stdin) is left as the inherited stdin-pipe read-end and is NEVER
    read. The daemon STAYS in the shell's process group (no ``setsid``) so a
    group-scoped ``killpg(proc.pid)`` reap WOULD reach it -- the only thing
    protecting it is that its drains EOF'd.

The shell-level parent exits 0 at once so ``proc.wait()`` sees "done".

Usage:
    python rt24_redir_daemon.py <pidfile> <logfile> <sleep_seconds>
"""

from __future__ import annotations

import os
import sys
import time

pidfile = sys.argv[1]
logfile = sys.argv[2]
sleep_s = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0

pid = os.fork()
if pid > 0:
    # Shell's direct child (the leader): exit 0 immediately.
    os._exit(0)

# Daemon grandchild. Redirect stdout+stderr onto a real FILE so the capture
# pipe write-ends are CLOSED in this process -> the parent's drains EOF at once
# (this is exactly what a redirected daemon does and why it must survive).
log_fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.dup2(log_fd, 1)
os.dup2(log_fd, 2)
os.close(log_fd)

# fd 0 (stdin) stays inherited and is deliberately NOT read. Record our pid
# and survive -- a real backgrounded service.
with open(pidfile, "w") as fh:
    fh.write(str(os.getpid()))
    fh.flush()

time.sleep(sleep_s)
os._exit(0)
