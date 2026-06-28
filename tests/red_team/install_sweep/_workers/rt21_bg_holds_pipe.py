"""Shell-free worker: fork a child that INHERITS stdout and lives on,
then the parent exits 0 immediately. The grandchild keeps the stdout
write-end open (no EOF) so the drain thread cannot see EOF.

argv[1] = seconds the backgrounded holder lives.
"""

import os
import sys
import time

dur = float(sys.argv[1])
pid = os.fork()
if pid == 0:
    # grandchild: keep stdout open, write nothing, just live
    time.sleep(dur)
    os._exit(0)
else:
    # parent (the "shell"-level child) exits immediately, success
    os._exit(0)
