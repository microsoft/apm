"""Copy-on-write file cloning (reflinks) for fast large-tree materialisation.

Modern filesystems (APFS on macOS, btrfs and XFS on Linux, ReFS on
Windows) support **copy-on-write clones** -- a metadata-only operation
that produces a new file referencing the same on-disk extents as the
source. The clone shares storage with the source until either side is
modified, at which point only the modified blocks are physically copied.

For ``apm install``, this turns the warm-cache materialisation step
(``cache/git/checkouts_v1/<sha>/`` -> ``apm_modules/<dep>/``) and the
primitive integration step (``apm_modules/<dep>/skills/`` ->
``.agents/skills/``) from byte-by-byte reads + writes into a handful of
inode operations. On supported filesystems the wall-time win is
typically 5x-20x for source trees of any non-trivial size.

Behaviour
---------
* On **macOS** (Darwin), uses ``clonefile(2)`` from libSystem via ctypes.
  Available on APFS, which is the default for macOS 10.13+.
* On **Linux**, uses the ``FICLONE`` ioctl. Supported on btrfs, XFS
  (``mkfs.xfs -m reflink=1``, default since xfsprogs 5.1), Bcachefs.
* On **all platforms** falls back to ``shutil.copy2`` when:
  - The platform has no clone primitive.
  - The filesystem does not support clones (cross-device, ext4, NFS, etc).
  - ``APM_NO_REFLINK=1`` is set (escape hatch).

The fallback is *transparent*: callers always get a usable copy. Reflinks
are an optimisation, never a correctness contract.

Capability cache
----------------
A successful or failed reflink probe is cached per ``st_dev`` so the
second file on a non-supporting filesystem skips the ctypes call entirely
and goes straight to the fallback. This keeps the overhead in the
no-reflink case to a single ``stat`` per destination directory.

API
---
* :func:`clone_file` -- attempt to reflink one file; return True on
  success.
* :func:`reflink_supported` -- best-effort runtime probe (exposed for
  tests and diagnostics).
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import os
import sys
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Module-level state: capability cache + ctypes bindings
# ---------------------------------------------------------------------------

# Map st_dev -> bool. True means clones have worked on this device,
# False means they have failed with a "not supported" errno.
# Devices not in the dict are unprobed; treat as "try once".
_device_capability: dict[int, bool] = {}
_capability_lock = threading.Lock()

# Lazy-initialised ctypes function for macOS clonefile(2).
_clonefile_fn: ctypes._FuncPointer | None = None
_clonefile_loaded: bool = False
_clonefile_lock = threading.Lock()

# FICLONE ioctl number on Linux. _IOW(0x94, 9, int) = 0x40049409 on all
# common architectures. Value is stable across glibc versions.
_FICLONE: int = 0x40049409

# Errnos that indicate the filesystem cannot service a clone request.
# These are sticky -- once we see them, we never retry on the same device.
_UNSUPPORTED_ERRNOS: frozenset[int] = frozenset(
    {
        errno.ENOTSUP,
        errno.EOPNOTSUPP,
        errno.EXDEV,
        errno.EINVAL,  # FICLONE on incompatible FS sometimes returns EINVAL
    }
)


# ---------------------------------------------------------------------------
# Platform-specific primitives
# ---------------------------------------------------------------------------


def _load_macos_clonefile() -> ctypes._FuncPointer | None:
    """Resolve and cache the libc ``clonefile`` symbol (macOS only)."""
    global _clonefile_fn, _clonefile_loaded
    if _clonefile_loaded:
        return _clonefile_fn
    with _clonefile_lock:
        if _clonefile_loaded:  # double-checked
            return _clonefile_fn
        try:
            libc_path = ctypes.util.find_library("c")
            if libc_path is None:
                _clonefile_loaded = True
                return None
            libc = ctypes.CDLL(libc_path, use_errno=True)
            fn = libc.clonefile
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
            fn.restype = ctypes.c_int
            _clonefile_fn = fn
        except (OSError, AttributeError):
            _clonefile_fn = None
        finally:
            _clonefile_loaded = True
        return _clonefile_fn


def _clone_macos(src: str, dst: str) -> bool:
    """Reflink ``src`` -> ``dst`` via macOS ``clonefile(2)``.

    Returns True on success. On failure, sets the destination's
    capability bit so the next call short-circuits to fallback.
    """
    fn = _load_macos_clonefile()
    if fn is None:
        return False
    # Flags = 0: follow symlinks, copy ACLs, copy ownership.
    rc = fn(src.encode("utf-8"), dst.encode("utf-8"), 0)
    if rc == 0:
        return True
    err = ctypes.get_errno()
    if err in _UNSUPPORTED_ERRNOS:
        _mark_device_unsupported(dst)
    return False


def _clone_linux(src: str, dst: str) -> bool:
    """Reflink ``src`` -> ``dst`` via Linux ``FICLONE`` ioctl.

    The destination must be created and opened O_WRONLY before issuing
    the ioctl. We open with mode 0o600 so ``shutil.copy2`` (the caller's
    fallback path) does not race with us on the metadata.
    """
    import fcntl

    src_fd: int | None = None
    dst_fd: int | None = None
    try:
        src_fd = os.open(src, os.O_RDONLY)
        # O_CREAT|O_EXCL: if dst exists we don't want to silently
        # overwrite (caller is responsible for clearing it first).
        dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            fcntl.ioctl(dst_fd, _FICLONE, src_fd)
            return True
        except OSError as exc:
            if exc.errno in _UNSUPPORTED_ERRNOS:
                _mark_device_unsupported(dst)
            # Remove the empty dst we created so the fallback path can
            # write its own copy without EEXIST.
            try:
                os.close(dst_fd)
                dst_fd = None
                os.unlink(dst)
            except OSError:
                pass
            return False
    except OSError:
        # open() failure (typically dst already exists) -- caller falls back.
        if dst_fd is not None:
            try:
                os.close(dst_fd)
                dst_fd = None
            except OSError:
                pass
            with contextlib.suppress(OSError):
                os.unlink(dst)
        return False
    finally:
        if src_fd is not None:
            with contextlib.suppress(OSError):
                os.close(src_fd)
        if dst_fd is not None:
            with contextlib.suppress(OSError):
                os.close(dst_fd)


# ---------------------------------------------------------------------------
# Capability cache
# ---------------------------------------------------------------------------


def _device_for(path: str) -> int | None:
    """Return ``st_dev`` for the parent of *path*, or None on stat failure."""
    parent = os.path.dirname(path) or "."
    try:
        return os.stat(parent).st_dev
    except OSError:
        return None


def _is_device_known_unsupported(path: str) -> bool:
    """Return True if a previous reflink attempt on this device failed."""
    dev = _device_for(path)
    if dev is None:
        return False
    with _capability_lock:
        return _device_capability.get(dev) is False


def _mark_device_unsupported(path: str) -> None:
    dev = _device_for(path)
    if dev is None:
        return
    with _capability_lock:
        _device_capability[dev] = False


def _mark_device_supported(path: str) -> None:
    dev = _device_for(path)
    if dev is None:
        return
    with _capability_lock:
        # Don't downgrade a False to True via a one-off fluke.
        _device_capability.setdefault(dev, True)


def _reset_capability_cache() -> None:
    """Test hook: clear the per-device capability cache."""
    with _capability_lock:
        _device_capability.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reflink_supported() -> bool:
    """Return True if this platform exposes a clone primitive at all.

    Does not probe any filesystem -- only checks that the OS-level
    syscall is reachable. Per-filesystem support is checked lazily
    inside :func:`clone_file`.
    """
    if os.environ.get("APM_NO_REFLINK"):
        return False
    if sys.platform == "darwin":
        return _load_macos_clonefile() is not None
    if sys.platform.startswith("linux"):
        return True  # FICLONE is in mainline since 4.5 (2016)
    return False


def clone_file(src: str | Path, dst: str | Path) -> bool:
    """Try to clone *src* to *dst* via filesystem reflink.

    Returns True on a successful clone. Returns False (without raising)
    when:
    * The platform has no clone primitive.
    * The filesystem does not support clones (sticky -- cached per device).
    * The destination already exists.
    * Any other clone error.

    On False, the caller MUST fall back to a real copy. This function
    deliberately never raises, so it can sit on the hot install path
    without try/except scaffolding at every call site.
    """
    if os.environ.get("APM_NO_REFLINK"):
        return False
    src_s = os.fspath(src)
    dst_s = os.fspath(dst)
    if _is_device_known_unsupported(dst_s):
        return False
    ok = False
    if sys.platform == "darwin":
        ok = _clone_macos(src_s, dst_s)
    elif sys.platform.startswith("linux"):
        ok = _clone_linux(src_s, dst_s)
    if ok:
        _mark_device_supported(dst_s)
    return ok
