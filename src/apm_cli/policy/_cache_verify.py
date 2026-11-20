"""Cached org policy verification and pinned-cache outcomes (#827, #4218).

Split out of ``policy/discovery.py`` (file-length guardrail); discovery's
``_read_cache_entry`` stays the only reader of the policy cache and asks
``_cached_policy_verified`` before it parses a cached document.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from .project_config import ProjectPolicyConfigError

if TYPE_CHECKING:
    from .discovery import PolicyFetchResult, _CacheEntry


def _cached_pin_matches(raw_bytes_hash: str, expected_hash: str | None) -> bool:
    """Whether a cache entry's recorded raw-bytes hash satisfies the project pin.

    Pin verification (#827): if the project pinned a hash and the cache was
    written without one (legacy entry) or with a different one, the entry is
    not used, so the fetcher verifies the pin against fresh authoritative
    bytes.
    """
    from .discovery import _split_hash_pin

    if expected_hash is None:
        return True
    try:
        exp_algo, exp_hex = _split_hash_pin(expected_hash)
    except ProjectPolicyConfigError:
        return False
    return raw_bytes_hash.lower() == f"{exp_algo}:{exp_hex}"


def _cached_policy_verified(serialized: str, meta: dict, expected_hash: str | None) -> bool:
    """Whether a cached policy document may be used (#827, #4218).

    The recorded raw-bytes hash must satisfy the project pin, and the cached
    document must be the one this entry recorded: content that no longer
    matches its fingerprint is never applied, so the caller fetches again and
    verifies the pin on fresh bytes.
    """
    raw_bytes_hash = meta.get("raw_bytes_hash", "") or ""
    return _cached_pin_matches(raw_bytes_hash, expected_hash) and _cached_policy_matches(
        serialized, meta
    )


def _cached_policy_matches(serialized: str, meta: dict) -> bool:
    """Whether a cached policy document is the one its metadata recorded."""
    from .discovery import _policy_fingerprint

    recorded = meta.get("fingerprint")
    return (
        isinstance(recorded, str)
        and bool(recorded)
        and hmac.compare_digest(recorded, _policy_fingerprint(serialized))
    )


def _unverified_pinned_cache(
    expected_hash: str,
    source_label: str,
    *,
    error: str = "Cached policy bytes do not match the project policy.hash pin",
) -> PolicyFetchResult:
    """A persisted cache entry failed pin verification and nothing replaced it."""
    from .discovery import PolicyFetchResult

    return PolicyFetchResult(
        source=source_label,
        outcome="hash_mismatch",
        error=error,
        expected_hash=expected_hash,
    )


def _unverifiable_cache_pin(
    expected_hash: str,
    source: str,
) -> PolicyFetchResult:
    """Fail closed when cache-only discovery cannot verify a project pin."""
    from .discovery import PolicyFetchResult

    return PolicyFetchResult(
        source=source,
        outcome="hash_mismatch",
        error="Policy hash pin cannot be verified without cached policy bytes",
        expected_hash=expected_hash,
    )


def _cache_only_policy_result(
    cache_entry: _CacheEntry | None,
    *,
    source_label: str,
    expected_hash: str | None,
    cache_entry_persisted: bool,
) -> PolicyFetchResult:
    """Return a cached policy or an offline-safe absent outcome."""
    from .discovery import PolicyFetchResult

    if cache_entry is None:
        if expected_hash is not None and cache_entry_persisted:
            return _unverified_pinned_cache(expected_hash, source_label)
        return PolicyFetchResult(source=source_label, outcome="absent")
    return PolicyFetchResult(
        policy=cache_entry.policy,
        source=cache_entry.source,
        cached=True,
        cache_stale=True,
        cache_age_seconds=cache_entry.age_seconds,
        outcome="cached_stale",
        raw_bytes_hash=cache_entry.raw_bytes_hash or None,
        expected_hash=expected_hash,
        warnings=cache_entry.warnings,
    )


def _stale_fallback_or_error(
    cache_entry: _CacheEntry | None,
    fetch_error_msg: str,
    source_label: str,
    outcome_on_miss: str,
    *,
    unverified_pin: str | None = None,
) -> PolicyFetchResult:
    """Return stale cache if available, otherwise error with given outcome.

    *unverified_pin* is the project ``policy.hash`` when a persisted cache
    entry exists but failed verification (#4218). If fresh bytes cannot be
    fetched either, the pin cannot be verified: that is a hash mismatch,
    which always blocks, never a fetch failure that may fall open.
    """
    from .discovery import PolicyFetchResult

    if cache_entry is None and unverified_pin is not None:
        return _unverified_pinned_cache(
            unverified_pin,
            source_label,
            error=(
                "Policy hash pin cannot be verified: the cached policy was not usable "
                f"and a fresh fetch failed ({fetch_error_msg})"
            ),
        )
    if cache_entry is not None:
        return PolicyFetchResult(
            policy=cache_entry.policy,
            source=cache_entry.source,
            cached=True,
            cache_stale=True,
            cache_age_seconds=cache_entry.age_seconds,
            fetch_error=fetch_error_msg,
            outcome="cached_stale",
            warnings=cache_entry.warnings,
        )
    return PolicyFetchResult(
        error=fetch_error_msg,
        source=source_label,
        fetch_error=fetch_error_msg,
        outcome=outcome_on_miss,
    )
