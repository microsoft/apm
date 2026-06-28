"""Round-30 worker: flood stdout AND stderr past the 1 MiB capture cap with
MULTIBYTE UTF-8 content, plus a deliberately INVALID UTF-8 byte run, to stress
the cap boundary + the text-mode decode in ``_drain_capped``.

The capture cap (``_MAX_CAPTURE_CHARS`` = 1 MiB) is a CHARACTER cap applied to
an already-decoded ``str`` (Popen text=True). This worker:

  * emits ``chars`` multibyte codepoints (default the 3-byte EUR sign) on BOTH
    fd 1 and fd 2 so the cap cut lands AFTER a whole codepoint near 1 MiB, and
  * splices a short run of RAW INVALID UTF-8 bytes (\\xff\\xfe) at byte offset
    ``bad_at`` so a decode error can straddle a 64 KiB read boundary.

Writes go via ``os.write`` to fds 1/2 directly (bytes), bypassing Python's
text-mode writer, so the installer's reader is the one that must decode/cap.

Secure contract under test: the install stays BOUNDED, capture is clamped to
<= 1 MiB/stream, and neither the cap cut nor the invalid bytes crash the drain
(``UnicodeDecodeError`` is a ``ValueError`` the drain must swallow).

Usage:
    python rt30_cap_flood.py <chars> <bad_at>
"""

from __future__ import annotations

import os
import sys

chars = int(sys.argv[1]) if len(sys.argv) > 1 else 1_300_000
bad_at = int(sys.argv[2]) if len(sys.argv) > 2 else 500_000

UNIT = "\u20ac".encode("utf-8")  # 3-byte EUR sign
BAD = b"\xff\xfe\xff\xfe"

payload = bytearray()
for i in range(chars):
    payload += UNIT
    if i == bad_at:
        payload += BAD

buf = bytes(payload)
# Interleave writes on both streams so both drains race the cap.
for fd in (1, 2):
    written = 0
    n = len(buf)
    while written < n:
        try:
            written += os.write(fd, buf[written : written + 65536])
        except BrokenPipeError:
            break
        except OSError:
            break

os._exit(0)
