"""Count how many times a subprocess persists ``apm.lock.yaml`` (Linux inotify).

The installed CLI is a black box: the only honest way to observe how often a
command writes the lockfile is to watch the project directory while it runs.
``LockFile.write`` publishes through an atomic rename (``IN_MOVED_TO``); a
direct write closes a writable handle (``IN_CLOSE_WRITE``). Reads produce
neither event, so the count is exactly the number of persisted writes.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_IN_Q_OVERFLOW = 0x00004000
_EVENT_HEADER = struct.Struct("iIII")

INOTIFY_AVAILABLE = sys.platform.startswith("linux")


@dataclass
class LockfileWriteCounter:
    """Accumulates persisted writes of one lockfile name in one directory."""

    name: str
    writes: int = 0
    events: list[str] = field(default_factory=list)


def _drain(fd: int, counter: LockfileWriteCounter) -> None:
    while True:
        try:
            buffer = os.read(fd, 1 << 20)
        except BlockingIOError:
            return
        offset = 0
        while offset < len(buffer):
            _wd, mask, _cookie, length = _EVENT_HEADER.unpack_from(buffer, offset)
            start = offset + _EVENT_HEADER.size
            name = buffer[start : start + length].rstrip(b"\0").decode("utf-8", "replace")
            offset = start + length
            if mask & _IN_Q_OVERFLOW:
                raise AssertionError("inotify queue overflowed; write count is unreliable")
            if name == counter.name and mask & (_IN_CLOSE_WRITE | _IN_MOVED_TO):
                counter.writes += 1
                counter.events.append(hex(mask))


@contextmanager
def count_lockfile_writes(
    directory: Path, name: str = "apm.lock.yaml"
) -> Iterator[LockfileWriteCounter]:
    """Yield a counter of persisted writes to ``directory/name`` while active."""
    if not INOTIFY_AVAILABLE:  # pragma: no cover -- guarded by callers
        raise RuntimeError("lockfile write counting requires Linux inotify")
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1 failed")
    try:
        watch = libc.inotify_add_watch(fd, os.fsencode(directory), _IN_CLOSE_WRITE | _IN_MOVED_TO)
        if watch < 0:
            raise OSError(ctypes.get_errno(), f"inotify_add_watch failed for {directory}")
        counter = LockfileWriteCounter(name=name)
        yield counter
        _drain(fd, counter)
    finally:
        os.close(fd)
