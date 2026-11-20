"""Serve the last-known org policy when an earlier candidate is unreachable (#4456).

Discovery tries ``.github-private`` before ``.github``. An absent candidate
(404) is not cached, so a cached ``.github`` policy means ``.github-private``
was absent when the policy was cached. Offline, ``.github-private`` cannot be
reached at all; that transport failure must not hide the cached policy. A
fresh entry is served as found, one past its TTL but within ``MAX_STALE_TTL``
as ``cached_stale``: exactly what that candidate's own refresh failure gives.
Auth and other errors still fail closed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from apm_cli.policy.discovery import PolicyFetchResult

_TRANSPORT_FAILURES = ("Connection error fetching", "Timeout fetching")


def cached_later_candidate(
    failed: PolicyFetchResult,
    repo_refs: Sequence[str],
    project_root: Path,
    *,
    expected_hash: str | None,
    skip: bool,
) -> PolicyFetchResult | None:
    """The first lower-precedence candidate's usable cache entry, else None."""
    from apm_cli.policy import discovery

    message = failed.fetch_error or failed.error or ""
    if skip or not message.startswith(_TRANSPORT_FAILURES):
        return None
    for repo_ref in repo_refs:
        entry = discovery._read_cache_entry(repo_ref, project_root, expected_hash=expected_hash)
        if entry is None:
            continue
        if entry.stale:
            return discovery._stale_fallback_or_error(
                entry, message, entry.source, "cache_miss_fetch_fail"
            )
        return discovery.PolicyFetchResult(
            policy=entry.policy,
            source=entry.source,
            cached=True,
            cache_age_seconds=entry.age_seconds,
            outcome="empty" if discovery._is_policy_empty(entry.policy) else "found",
            raw_bytes_hash=entry.raw_bytes_hash or None,
            expected_hash=expected_hash,
            warnings=entry.warnings,
        )
    return None
