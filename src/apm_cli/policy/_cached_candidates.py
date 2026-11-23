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


def record(result: PolicyFetchResult, refs: Sequence[str], index: int, project_root: Path) -> None:
    """Record a fresh resolution: the source, and the candidates absent above it."""
    from apm_cli.cache.paths import cache_writes_allowed
    from apm_cli.policy import discovery

    if result.policy is None or result.cached or result.cache_stale or not cache_writes_allowed():
        return
    path = discovery._resolution_record_path(list(refs), project_root)
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
        record_path = discovery._resolution_record_path(list(refs), project_root)
        data = json.loads(record_path.read_text(encoding="utf-8"))
        source, absent = data["source"], set(data["absent"])
    except (OSError, ValueError, KeyError, TypeError):
        return None  # no proof of the last resolution: the failure stands
    if source not in refs[index + 1 :] or not set(refs[index : refs.index(source)]) <= absent:
        return None
    entry = discovery._read_cache_entry(source, project_root, expected_hash=expected_hash)
    if entry is None:
        return None
    return discovery._cached_entry_result(entry, expected_hash, message)
