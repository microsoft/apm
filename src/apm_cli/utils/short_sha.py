"""SHA short-form helper for user-facing install output (F3, #1116).

The install pipeline prints commit SHAs on every download/cached line
(e.g. ``[+] owner/repo@v1 abc12345 (cached)``). Historically, every
call site did its own ``commit[:8]`` slice -- which silently truncated
sentinel strings like ``"unknown"`` to ``"unknown\u200b"``-looking
gibberish, and would happily crop a non-hex value, hiding upstream
bugs from review.

``format_short_sha`` centralises the truncation with one rule:
- Return ``""`` when the input is ``None``, not a ``str``, the literal
  ``"cached"`` / ``"unknown"`` sentinels, or shorter than 8 chars, or
  not pure hex.
- Otherwise return the first 8 characters.

Returning the empty string lets callers skip the SHA suffix without
special-casing each render path.
"""

from __future__ import annotations

_HEX = frozenset("0123456789abcdefABCDEF")
_SENTINELS = frozenset({"cached", "unknown"})


def format_short_sha(value: object) -> str:
    """Return an 8-char short SHA or ``""`` for invalid inputs.

    Args:
        value: Anything; non-string inputs and sentinels collapse to
            ``""``. Real Git SHAs are 40 chars (SHA-1) or 64 chars
            (SHA-256); both are accepted, as are any hex string of
            length >= 8 to keep the helper future-proof for short-hash
            contexts.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or candidate.lower() in _SENTINELS:
        return ""
    if len(candidate) < 8:
        return ""
    if not all(ch in _HEX for ch in candidate):
        return ""
    return candidate[:8]
