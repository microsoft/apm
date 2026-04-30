"""Shared git-related utilities for marketplace modules."""

from __future__ import annotations

import re

__all__ = ["redact_token"]

# Redact auth tokens from git URLs in error messages and logs.
# Covers: https://TOKEN@host, http://TOKEN@host, and ?token=VALUE query params.
_TOKEN_RE = re.compile(r"https?://[^@\s]*@|([?&])token=[^\s&]*")


def redact_token(text: str) -> str:
    """Replace auth tokens in *text* with redacted placeholders."""
    return _TOKEN_RE.sub(
        lambda m: "https://***@" if "@" in m.group() else f"{m.group(1)}token=***",
        text,
    )
