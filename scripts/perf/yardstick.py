"""Runner CPU yardstick (research only): process CPU of fixed pure-Python, hash and fs work."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time


def timed(fn) -> float:
    start = time.process_time()
    fn()
    return round(time.process_time() - start, 4)


def py_loop() -> None:
    total = 0
    for i in range(8_000_000):
        total += i * i % 7
    assert total


def sha() -> None:
    block = b"x" * (1 << 20)
    digest = hashlib.sha256()
    for _ in range(256):
        digest.update(block)


def fs() -> None:
    root = tempfile.mkdtemp()
    try:
        for i in range(2000):
            path = os.path.join(root, f"f{i}.txt")
            with open(path, "wb") as handle:
                handle.write(b"y" * 1024)
            os.stat(path)
            os.path.realpath(path)
    finally:
        shutil.rmtree(root)


def main() -> int:
    rows = {
        name: min(timed(fn) for _ in range(3))
        for name, fn in (("py_loop_8M", py_loop), ("sha256_256MB", sha), ("fs_2000_files", fs))
    }
    json.dump(rows, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
