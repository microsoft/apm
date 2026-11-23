"""One fingerprint of the effective org policy a command ran under (#4940).

The install gate writes it into each package render record; an audit replay
compares it with the fingerprint of its own discovery. Equal fingerprints mean
the same discovery outcome and the same merged effective policy. An outcome
whose effective policy is not known for certain (a stale cache, a failed or
malformed fetch, a hash mismatch, an incomplete chain) has no fingerprint, so
it can never match anything.
"""

from __future__ import annotations

import hashlib
from typing import Any

# Outcomes whose effective policy is fully known; each fingerprints distinctly.
_KNOWN_OUTCOMES = frozenset({"found", "empty", "absent", "no_git_remote", "disabled"})
_DOMAIN = "apm-effective-policy/v1\0"


def effective_policy_fingerprint(fetch: Any | None, *, disabled: bool = False) -> str | None:
    """The fingerprint of *fetch* (a ``PolicyFetchResult``), or ``None`` when uncertain.

    *disabled* marks a run whose policy gate was turned off: it has its own
    fingerprint, distinct from every discovery outcome.
    """
    if disabled:
        return _digest("gate:disabled")
    if fetch is None:
        return None
    outcome = getattr(fetch, "outcome", None)
    if outcome not in _KNOWN_OUTCOMES or getattr(fetch, "cache_stale", False):
        return None
    if getattr(fetch, "fetch_error", None):
        return None
    policy = getattr(fetch, "policy", None)
    if outcome in ("found", "empty"):
        # A policy served alongside an error is not the effective policy for certain.
        if policy is None or getattr(fetch, "error", None):
            return None
        from .discovery import _serialize_policy

        return _digest("policy:" + _serialize_policy(policy))
    return _digest("outcome:" + outcome)


def _digest(body: str) -> str:
    return hashlib.sha256((_DOMAIN + body).encode("utf-8")).hexdigest()
