"""Worker: flood stdout AND stderr past the cap while reading nothing.

argv[1] = total bytes per stream. Does NOT read stdin (tests the stdin
feeder + dual-stream-over-cap deadlock case).
"""

import sys

n = int(sys.argv[1])
chunk = "A" * (1 << 20)
full, rem = divmod(n, len(chunk))
for _ in range(full):
    sys.stdout.write(chunk)
    sys.stderr.write(chunk)
sys.stdout.write("A" * rem)
sys.stderr.write("A" * rem)
sys.stdout.flush()
sys.stderr.flush()
