"""Native persistent volume identity, without invoking an external command."""

from __future__ import annotations

import ctypes
import os
import uuid
from pathlib import Path


class _AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


def darwin_volume_uuid(path: Path) -> str:
    """Read ATTR_VOL_UUID; fail closed if a stable identity is unavailable."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    getattrlist = libc.getattrlist
    getattrlist.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(_AttrList),
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_ulong,
    ]
    getattrlist.restype = ctypes.c_int
    # ATTR_VOL_INFO | ATTR_VOL_UUID; the packed result is a 32-bit length
    # followed by the 16-byte UUID. getattrlist accepts a path within a volume.
    attrs = _AttrList(5, 0, 0, 0x80040000, 0, 0, 0)
    result = ctypes.create_string_buffer(20)
    if getattrlist(os.fsencode(path), ctypes.byref(attrs), result, len(result), 0):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(path))
    if int.from_bytes(result.raw[:4], "little") != 20:
        raise ValueError("Invalid persistent volume identity response")
    volume = uuid.UUID(bytes=result.raw[4:20])
    if volume.int == 0:
        raise ValueError("Persistent volume identity unavailable")
    return str(volume)
