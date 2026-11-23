"""Policy freshness and policy-age templating for the verified-state record (#4977).

Split out of ``install/verdict_record.py`` verbatim (install LOC budget).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from apm_cli.install.verdict_record import (
    _GOVERNED_DIRS,
    _cache_root,
    _read_nofollow,
    _walk_files,
)

# O1: a cached policy's age (CommandLogger.policy_resolved) is templated to its
# cache entry and rendered from that entry's live cached_at at replay.
_AGE_RE = re.compile(r"(fetched )(\d+[smh])( ago)")
_AGE_TOKEN_RE = re.compile("\x00age:([^\x00]+)\x00")
_AGE_LAG = 900  # the policy line prints at discovery; the writer runs up to this much later

# A record expires within a day, and when its earliest cached policy goes stale
# for discovery (cached_at + capped_ttl(cache.ttl)), so a refetch is never
# replayed past (W3d R2).
_RECORD_MAX_AGE = 24 * 3600


def _policy_fresh_until(meta_path: Path) -> float:
    """When discovery stops trusting one cached policy (policy/discovery.py)."""
    from apm_cli.policy._cache_verify import capped_ttl
    from apm_cli.policy.parser import load_policy

    meta = json.loads(_read_nofollow(meta_path) or b"{}")
    document = meta_path.with_name(meta_path.name[: -len(".meta.json")] + ".yml")
    try:
        policy, _warnings = load_policy(document.read_text(encoding="utf-8"))
        ttl = capped_ttl(int(policy.cache.ttl))
    except Exception:  # an unreadable policy is never fresh: the record is born expired
        return 0.0
    governed = policy.unmanaged_files.directories or ()
    if any(str(d).strip("/").split("/")[0] not in _GOVERNED_DIRS for d in governed):
        # The commit gate scans these directories; the record keys only target roots.
        raise ValueError("policy governs unmanaged files outside the target roots")
    return float(meta.get("cached_at", 0)) + ttl


def _policy_metas() -> list[tuple[str, float]]:
    cache = _cache_root()
    if cache is None:
        return []
    policy = cache / "policy_v1"
    metas = []
    for path in _walk_files(policy):
        if path.name.endswith(".meta.json"):
            meta = json.loads(_read_nofollow(path) or b"{}")
            metas.append((path.relative_to(policy).as_posix(), float(meta.get("cached_at", 0))))
    return metas


def _template_ages(text: str) -> str | None:
    """Each printed policy age becomes a token naming its cache entry; None if unsure."""
    if not _AGE_RE.search(text):
        return text
    from apm_cli.core.command_logger import format_policy_age

    now, metas = time.time(), _policy_metas()

    def token(match: re.Match[str]) -> str:
        found = {
            rel
            for rel, cached_at in metas
            for lag in range(_AGE_LAG)
            if format_policy_age(int(now - cached_at) - lag) == match.group(2)
        }
        if len(found) != 1:
            raise ValueError("policy age")
        return f"{match.group(1)}\x00age:{found.pop()}\x00{match.group(3)}"

    try:
        return _AGE_RE.sub(token, text)
    except ValueError:
        return None


def _render_ages(text: str) -> str:
    """The policy line's age now, from the live cache entry the record names."""
    if "\x00age:" not in text:
        return text
    from apm_cli.core.command_logger import format_policy_age

    cache = _cache_root()
    if cache is None:
        raise ValueError("no cache")

    def age(match: re.Match[str]) -> str:
        rel = match.group(1)
        if ".." in rel.split("/"):
            raise ValueError("policy age path")
        meta = json.loads(_read_nofollow(cache / "policy_v1" / rel) or b"{}")
        seconds = int(time.time() - float(meta["cached_at"]))
        if seconds < 0:
            raise ValueError("policy age")
        return format_policy_age(seconds)

    return _AGE_TOKEN_RE.sub(age, text)


def _expiry(issued: float) -> float:
    expiry = issued + _RECORD_MAX_AGE
    cache = _cache_root()
    if cache is not None:
        for path in _walk_files(cache / "policy_v1"):
            if path.name.endswith(".meta.json"):
                expiry = min(expiry, _policy_fresh_until(path))
    return expiry
