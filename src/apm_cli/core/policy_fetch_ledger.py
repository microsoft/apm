"""Org-policy fetch attempts made by this process.

Each fetch channel notes its attempt when it decides to fetch, before any
credential lookup or request, so a run whose policy came only from the cache
can be told apart from one that went to the network (the verified-state record
replays only the former). Import-light on purpose: nothing here may pull in
the HTTP stack.
"""

from __future__ import annotations

_ATTEMPTS: list[str] = []


def note_fetch_attempt(channel: str) -> None:
    """Record that *channel* (``github``, ``gitlab``, ``ado`` or ``http``) is about to fetch."""
    _ATTEMPTS.append(channel)


def fetch_attempts() -> int:
    """How many policy fetch attempts this process has made so far."""
    return len(_ATTEMPTS)
