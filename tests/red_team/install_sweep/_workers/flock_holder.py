"""Real helper: grab the round-25 rotation lock file and hold it.

Usage: flock_holder.py <lock_path> <hold_seconds> <ready_path>

Opens the dedicated rotation lock file exactly as the target does
(O_WRONLY|O_CREAT|O_NOFOLLOW, 0600), takes an exclusive fcntl.flock,
writes <ready_path> to signal the parent the lock is held, then sleeps.
Used to prove whether a second rotator blocks (LOCK_EX, no LOCK_NB).
"""

import fcntl
import os
import sys
import time


def main() -> int:
    lock_path, hold_s, ready_path = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    with open(ready_path, "w", encoding="ascii") as fh:
        fh.write("held")
    time.sleep(hold_s)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
