"""Shared git-related utilities for marketplace modules."""

from __future__ import annotations

__all__ = ["redact_token"]


def redact_token(text: str) -> str:
    """Delegate Git diagnostic redaction to its canonical owner."""
    from ..utils.git_env import redact_git_diagnostic

    return redact_git_diagnostic(text)
