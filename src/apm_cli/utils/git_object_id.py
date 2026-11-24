"""Git object ids: interop with git's object format, not a security hash.

The integrity of locked content is the lock's SHA-256 ``content_hash``. These
helpers only recompute the id git itself assigns to an object (SHA-1 or
SHA-256, by the repository's object format), so APM can check that the bytes
``git cat-file`` returned are the object they claim to be.
"""

from __future__ import annotations

import hashlib

_HEX = frozenset("0123456789abcdef")


def object_format(oid: str) -> str:
    """Return ``"sha1"`` or ``"sha256"`` for a full lowercase hex object id."""
    if len(oid) not in (40, 64) or not set(oid) <= _HEX:
        raise ValueError("not a full object id")
    return "sha256" if len(oid) == 64 else "sha1"


def git_object_id(fmt: str, kind: str, data: bytes) -> str:
    """Return the id git assigns to a *kind* object holding *data* in format *fmt*."""
    header = f"{kind} {len(data)}\0".encode()
    return hashlib.new(fmt, header + data, usedforsecurity=False).hexdigest()
