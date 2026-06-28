"""Worker: write raw invalid UTF-8 bytes to stdout, then flood, exit 0."""

import os
import sys

# raw invalid utf-8 to the underlying fd 1
os.write(1, b"\xff\xfe\x80\x81" * 4096)
os.write(1, b"B" * (3 << 20))
sys.exit(0)
