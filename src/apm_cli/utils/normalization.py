"""Bytes-in / bytes-out content normalization helpers.

Used by drift-detection (``apm audit``) to compare a deployed file's
on-disk bytes against the replay scratch tree without flagging
legitimate, deterministic differences:

* Line-ending differences (CRLF vs LF) introduced by editors / VCS.
* UTF-8 BOMs at the start of the file (Windows tool output).
* APM ``<!-- Build ID: <sha> -->`` headers that are intentionally
  re-stamped on every recompile.

Kept in ``utils/`` (not ``install/drift.py``) so future callers --
e.g. policy linters or content-scan helpers -- can reuse the same
normalization without importing the drift module.
"""

from __future__ import annotations

import re

_BUILD_ID_PATTERN = re.compile(
    rb"<!--\s*Build ID:\s*[a-f0-9]+\s*-->\s*\n?",
    re.IGNORECASE,
)
_BOM = b"\xef\xbb\xbf"


def _strip_build_id(content: bytes) -> bytes:
    """Remove APM ``<!-- Build ID: <sha> -->`` headers wherever they appear."""
    return _BUILD_ID_PATTERN.sub(b"", content)


def _normalize_line_endings(content: bytes) -> bytes:
    """Convert CRLF to LF; leaves bare CR alone (rare, intentional)."""
    return content.replace(b"\r\n", b"\n")


def _strip_bom(content: bytes) -> bytes:
    """Drop a UTF-8 BOM at the start of the file (only at offset 0)."""
    if content.startswith(_BOM):
        return content[len(_BOM) :]
    return content


def _normalize(content: bytes) -> bytes:
    """Apply all drift-tolerant normalizations to a file's bytes."""
    return _strip_build_id(_normalize_line_endings(_strip_bom(content)))


__all__ = [
    "_BOM",
    "_BUILD_ID_PATTERN",
    "_normalize",
    "_normalize_line_endings",
    "_strip_bom",
    "_strip_build_id",
]
