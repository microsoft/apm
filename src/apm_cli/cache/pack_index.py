"""Whether a cached repository's pack holds a commit, read from its pack index.

``git cat-file -t <sha>`` answers from the same bytes: the pack ``.idx`` (v2)
names every packed object and its offset, and the entry header at that offset
in the ``.pack`` carries the object type. Reading them in process saves one
spawn per dependency on the cached-commit checks.

The answer is ``True``/``False`` only for a plain SHA-1 repository whose object
is a non-delta pack entry in a pack that passes git's own index checks. Any
other shape (loose object, alternates, replace refs or grafts, a
multi-pack-index, a delta entry, 64-bit offsets, a damaged or unexpected
index, not found) returns ``None`` and the caller spawns git as before. The
checkout itself is still verified by ``cache/checkout_verify``.
"""

from __future__ import annotations

import bisect
import mmap
import os
import struct
from pathlib import Path

_IDX_V2 = b"\xfftOc\x00\x00\x00\x02"
_FANOUT = 8 + 4 * 256
_COMMIT, _OFS_DELTA, _REF_DELTA = 1, 6, 7


def _refused(git_dir: Path, sha: str) -> bool:
    """Shapes the index alone cannot answer, so git must."""
    objects = git_dir / "objects"
    info = objects / "info"
    if any(
        (base / name).exists()
        for base, name in (
            (info, "alternates"),
            (info, "http-alternates"),
            (git_dir / "info", "grafts"),
            (objects / "pack", "multi-pack-index"),
            (objects / sha[:2], sha[2:]),
        )
    ):
        return True
    replace = git_dir / "refs" / "replace"
    if replace.exists() and any(replace.rglob("*")):
        return True
    try:
        with open(git_dir / "packed-refs", "rb") as handle:
            return any(b" refs/replace/" in line for line in handle)
    except FileNotFoundError:
        return False


def _index_lookup(idx, want: bytes) -> tuple[int, bytes] | None:
    """``(pack offset, pack checksum)`` for *want*, ``None`` when this index lacks it.

    Raises ValueError for an index git would reject (no answer at all then).
    """
    if len(idx) < _FANOUT + 40 or idx[:8] != _IDX_V2:
        raise ValueError("pack index")
    fanout = struct.unpack(">256I", idx[8:_FANOUT])
    if any(fanout[i] > fanout[i + 1] for i in range(255)):
        raise ValueError("pack index fanout")
    total = fanout[255]
    # git's index size check (packfile.c): no more, no less than the tables need.
    minimum = _FANOUT + total * (20 + 4 + 4) + 40
    maximum = minimum + (8 * (total - 1) if total else 0)
    if not minimum <= len(idx) <= maximum:
        raise ValueError("pack index size")
    lo = fanout[want[0] - 1] if want[0] else 0
    hi = fanout[want[0]]
    names = _FANOUT
    keys = [idx[names + 20 * i : names + 20 * i + 20] for i in range(lo, hi)]
    at = bisect.bisect_left(keys, want)
    if at == len(keys) or keys[at] != want:
        return None
    offsets = names + 24 * total
    (offset,) = struct.unpack(">I", idx[offsets + 4 * (lo + at) : offsets + 4 * (lo + at) + 4])
    if offset & 0x80000000:
        return None  # a 64-bit offset table entry
    return offset, idx[-40:-20]


def _entry_type(pack: Path, offset: int, checksum: bytes, objects: int) -> int | None:
    """The object type at *offset* of a pack whose header and trailer match its index."""
    with open(pack, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        header = handle.read(12)
        if size < offset + 1 or size < 32 or header[:4] != b"PACK":
            return None
        version, count = struct.unpack(">II", header[4:12])
        if version not in (2, 3) or count != objects:
            return None
        handle.seek(size - 20)
        if handle.read(20) != checksum:
            return None
        handle.seek(offset)
        first = handle.read(1)
    return (first[0] >> 4) & 7 if first else None


def packed_commit(git_dir: Path, sha: str) -> bool | None:
    """Whether a pack in *git_dir* holds *sha* as a commit; ``None`` when git must say."""
    try:
        if len(sha) != 40:
            return None
        sha = sha.lower()
        want = bytes.fromhex(sha)
        if _refused(git_dir, sha):
            return None
        for idx_path in sorted((git_dir / "objects" / "pack").glob("pack-*.idx")):
            with (
                open(idx_path, "rb") as handle,
                mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as idx,
            ):
                found = _index_lookup(idx, want)
                objects = struct.unpack(">I", idx[_FANOUT - 4 : _FANOUT])[0] if found else 0
            if found is None:
                continue
            kind = _entry_type(idx_path.with_suffix(".pack"), found[0], found[1], objects)
            if kind is None or kind in (_OFS_DELTA, _REF_DELTA):
                return None
            return kind == _COMMIT
        return None
    except (OSError, ValueError, struct.error, IndexError):
        return None


def differential() -> bool:
    """Under APM_DEBUG, callers also ask git and raise on any difference."""
    return bool(os.environ.get("APM_DEBUG"))


class PackIndexMismatch(AssertionError):
    """The differential found an index answer git disagrees with (never swallowed)."""


def check(answer: bool | None, spawned: bool) -> None:
    """The differential: an index answer must equal git's."""
    if answer is not None and answer != spawned:
        raise PackIndexMismatch("pack index answer differs from git cat-file -t")
