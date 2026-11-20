"""Serve the last-resolved org policy when a higher candidate is unreachable (#4456).

Discovery tries ``.github-private`` before ``.github``. When a fresh fetch
resolves a candidate, the candidates above it were absent (404); that
resolution is recorded beside the policy cache. Offline, a higher candidate
cannot be reached at all. The cascade falls through ONLY when the record
proves that the unreachable candidate was absent and names the lower source
whose cache entry exists: a fresh entry is served as found, one past its TTL
but within ``MAX_STALE_TTL`` as ``cached_stale``. Without that proof the
failure stands (``fetch_failure_default: block`` fails closed), so blocking
the network to a private policy repo can never downgrade to a public one.
Auth and other errors never fall through.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from apm_cli.policy.discovery import PolicyFetchResult

_TRANSPORT_FAILURES = ("Connection error fetching", "Timeout fetching")


def candidate_refs(org: str, host: str, candidates: Sequence[str]) -> list[str]:
    """Return the cache and fetch key of each GitHub-family policy candidate."""
    prefix = f"{host}/" if host and host != "github.com" else ""
    return [f"{prefix}{org}/{repo}" for repo in candidates]


def _record_path(refs: Sequence[str], project_root: Path) -> Path:
    from apm_cli.policy import discovery

    key = discovery._cache_key("resolution:" + "|".join(refs))
    return discovery._get_cache_dir(project_root) / f"{key}.resolution.json"


def record(result: PolicyFetchResult, refs: Sequence[str], index: int, project_root: Path) -> None:
    """Record a fresh resolution: the source, and the candidates absent above it."""
    if result.outcome not in ("found", "empty") or result.cached:
        return
    path = _record_path(refs, project_root)
    data = {"source": refs[index], "absent": list(refs[:index])}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        return


def after_failure(
    failed: PolicyFetchResult,
    refs: Sequence[str],
    index: int,
    project_root: Path,
    expected_hash: str | None,
) -> PolicyFetchResult | None:
    """The recorded source's cache when *failed* is an unreachable absent candidate."""
    from apm_cli.policy import discovery

    message = failed.fetch_error or failed.error or ""
    if failed.outcome != "cache_miss_fetch_fail" or not message.startswith(_TRANSPORT_FAILURES):
        return None
    try:
        data = json.loads(_record_path(refs, project_root).read_text(encoding="utf-8"))
        source, absent = data["source"], set(data["absent"])
    except (OSError, ValueError, KeyError, TypeError):
        return None  # no proof of the last resolution: the failure stands
    if source not in refs[index + 1 :] or not set(refs[index : refs.index(source)]) <= absent:
        return None
    entry = discovery._read_cache_entry(source, project_root, expected_hash=expected_hash)
    if entry is None:
        return None
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
