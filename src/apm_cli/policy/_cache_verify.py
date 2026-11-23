"""Cached org policy verification and pinned-cache outcomes (#827, #4218).

Split out of ``policy/discovery.py`` (file-length guardrail); discovery's
``_read_cache_entry`` stays the only reader of the policy cache and asks
``_cached_policy_verified`` before it parses a cached document.
"""

from __future__ import annotations

import hmac
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from ..utils.file_ops import robust_replace
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


def _cached_policy_verified(serialized: str | None, meta: dict, expected_hash: str | None) -> bool:
    """Whether a cached policy document may be used (#827, #4218).

    The recorded raw-bytes hash must satisfy the project pin, and the cached
    document must be the one this entry recorded: content that no longer
    matches its fingerprint is never applied, so the caller fetches again and
    verifies the pin on fresh bytes.
    """
    raw_bytes_hash = meta.get("raw_bytes_hash", "") or ""
    if serialized is None:
        return False
    if expected_hash is not None:  # ``cached_document`` re-hashed the leaf bytes
        from .parser import load_policy

        # A leaf that extends others needs its parents resolved: fetch again.
        leaf, _warnings = load_policy(serialized)
        return _cached_pin_matches(raw_bytes_hash, expected_hash) and leaf.extends is None
    return _cached_pin_matches(raw_bytes_hash, expected_hash) and _cached_policy_matches(
        serialized, meta
    )


# A cached document never keeps itself fresh for longer than this.
MAX_FRESH_TTL = 24 * 3600
# Cache files larger than this are not read; the policy is fetched again.
MAX_CACHE_FILE_BYTES = 1 << 20
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)


def read_cache_file(path: Path) -> bytes | None:
    """The bytes of a regular cache file of bounded size, else ``None`` (never a link or pipe)."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, _READ_FLAGS)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CACHE_FILE_BYTES:
            return None
        data = handle.read(MAX_CACHE_FILE_BYTES + 1)
    return data if len(data) <= MAX_CACHE_FILE_BYTES else None


def load_cache_meta(meta_file: Path) -> dict | None:
    """A cache entry's metadata, or ``None`` when it cannot be read as a JSON object."""
    data = read_cache_file(meta_file)
    try:
        meta = json.loads(data.decode("utf-8")) if data is not None else None
    except (UnicodeDecodeError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def capped_ttl(ttl: int) -> int:
    """The freshness a cached policy may claim for itself, bounded in code."""
    return min(ttl, MAX_FRESH_TTL)


def _raw_leaf_path(policy_file: Path) -> Path:
    return policy_file.with_suffix(".raw")


def store_raw_leaf(policy_file: Path, content: str | bytes | None, uid: str) -> None:
    """Keep the fetched leaf bytes beside a cache entry, or drop bytes it no longer has."""
    raw_file = _raw_leaf_path(policy_file)
    if content is None:
        raw_file.unlink(missing_ok=True)
        return
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    tmp = raw_file.with_name(f"{raw_file.name}.{uid}.tmp")
    try:
        tmp.write_bytes(data)
        robust_replace(tmp, raw_file)
    except OSError:
        tmp.unlink(missing_ok=True)
        raw_file.unlink(missing_ok=True)


def cached_document(
    policy_file: Path, meta: dict, expected_hash: str | None, repo_ref: str = ""
) -> str | None:
    """The text a cache entry may be parsed from, or ``None`` to fetch again.

    Unpinned, it is the stored document (``_cached_policy_verified`` then checks
    its recorded fingerprint). Pinned, it is the fetched leaf bytes kept with the
    entry, re-hashed against the pin. An entry composed from a policy chain or
    a namespace ancestry has no single leaf to verify, so it is fetched again.
    """
    if expected_hash is None:
        document = read_cache_file(policy_file)
        if document is None:
            return None
        # Written in text mode: read back with universal newlines, as it was hashed.
        return document.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    from .discovery import _verify_hash_pin

    chain = meta.get("chain_refs")
    recorded = meta.get("raw_bytes_hash", "") or ""
    if not isinstance(chain, list) or len(chain) != 1 or repo_ref.endswith("#ancestry"):
        return None
    if not _cached_pin_matches(recorded, expected_hash):
        return None
    raw = read_cache_file(_raw_leaf_path(policy_file))
    if raw is None:
        return None
    if _verify_hash_pin(raw, expected_hash, "cache") is not None:
        return None
    return raw.decode("utf-8")


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
