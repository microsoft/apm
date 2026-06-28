"""Worker: sleep forever (or argv[1] secs) without reading stdin/output.

Writes its own pid then sleeps. Used for timeout-reap + orphan probes.
"""

import sys
import time

dur = float(sys.argv[1]) if len(sys.argv) > 1 else 3600.0
time.sleep(dur)
