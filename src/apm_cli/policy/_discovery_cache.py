"""Cache I/O and hash-pin verification helpers for policy discovery.

Leaf module -- does NOT import ``discovery.py`` at module scope.
``PolicyFetchResult`` (defined in ``discovery.py``) is imported
function-locally inside the three helpers that return it so the
import graph stays acyclic.

Symbols that tests patch via ``apm_cli.policy.discovery.<NAME>``
remain patchable because ``discovery.py`` re-exports every public
name from this module with the ``NAME as NAME`` redundant-alias form.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..utils.path_security import PathTraversalError, ensure_path_within
from .parser import load_policy
from .project_config import (
    _DEFAULT_HASH_ALGORITHM,
    _HASH_HEX_LEN,
    _HEX_RE,
    ALLOWED_HASH_ALGORITHMS,
    ProjectPolicyConfigError,
    compute_policy_hash,
)
from .schema import ApmPolicy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache constants
# ---------------------------------------------------------------------------

POLICY_CACHE_DIR = ".policy-cache"
DEFAULT_CACHE_TTL = 3600  # 1 hour
MAX_STALE_TTL = 7 * 24 * 3600  # 7 days -- stale cache usable on refresh failure
CACHE_SCHEMA_VERSION = "3"  # Bump when cache format changes to auto-invalidate


# ---------------------------------------------------------------------------
# Hash-pin helpers
# ---------------------------------------------------------------------------


def _split_hash_pin(expected_hash: str) -> tuple[str, str]:
    """Split an ``"<algo>:<hex>"`` pin into (algorithm, lowercase_hex).

    Bare hex (no prefix) is interpreted as sha256 for backwards
    compatibility -- callers that care about the algorithm should pass a
    fully-qualified pin. Raises :class:`ProjectPolicyConfigError` on a
    structurally invalid pin (unsupported algorithm, wrong length, non
    hex). The discovery helpers translate that into a fail-closed
    ``hash_mismatch`` outcome rather than crashing.
    """
    raw = expected_hash.strip()
    if ":" in raw:
        algo, _, hex_part = raw.partition(":")
        algo = algo.strip().lower()
    else:
        algo = _DEFAULT_HASH_ALGORITHM
        hex_part = raw
    hex_part = hex_part.strip().lower()
    if algo not in ALLOWED_HASH_ALGORITHMS:
        raise ProjectPolicyConfigError(f"Unsupported policy.hash algorithm '{algo}'")
    expected_len = _HASH_HEX_LEN[algo]
    if len(hex_part) != expected_len or not _HEX_RE.match(hex_part):
        raise ProjectPolicyConfigError(f"policy.hash is not a valid {algo} digest")
    return algo, hex_part


def _compute_hash_normalized(content: str, expected_hash: str | None) -> str:
    """Compute the digest of *content* under the algorithm declared by
    *expected_hash*, returning the canonical ``"<algo>:<hex>"`` form.

    When *expected_hash* is ``None`` the default algorithm (sha256) is
    used so the cache always carries a digest for later pin verification.
    """
    algo = _DEFAULT_HASH_ALGORITHM
    if expected_hash:
        try:
            algo, _ = _split_hash_pin(expected_hash)
        except ProjectPolicyConfigError:
            algo = _DEFAULT_HASH_ALGORITHM
    digest = compute_policy_hash(content, algo)
    return f"{algo}:{digest}"


def _verify_hash_pin(
    content: object,
    expected_hash: str | None,
    source_label: str,
) -> object:  # PolicyFetchResult | None
    """Verify fetched policy bytes against the project's pin (#827).

    Returns ``None`` when there is no pin, or the digest matches. On
    mismatch -- or on a structurally invalid pin, which is treated as a
    mismatch to stay fail-closed -- returns a :class:`PolicyFetchResult`
    with ``outcome="hash_mismatch"`` that callers must propagate.
    """
    # Deferred import: PolicyFetchResult lives in discovery.py; importing it
    # here at module scope would create a cycle.
    from .discovery import PolicyFetchResult

    if expected_hash is None:
        return None

    raw_bytes: bytes
    if isinstance(content, bytes):
        raw_bytes = content
    elif isinstance(content, str):
        raw_bytes = content.encode("utf-8")
    else:
        return PolicyFetchResult(
            outcome="hash_mismatch",
            source=source_label,
            error=(
                f"Policy hash mismatch from {source_label}: "
                "no content available to verify against pin"
            ),
            expected_hash=expected_hash,
        )

    try:
        algo, expected_hex = _split_hash_pin(expected_hash)
    except ProjectPolicyConfigError as exc:
        return PolicyFetchResult(
            outcome="hash_mismatch",
            source=source_label,
            error=(f"Policy hash mismatch from {source_label}: invalid pin ({exc})"),
            expected_hash=expected_hash,
        )

    digest = hashlib.new(algo)
    digest.update(raw_bytes)
    actual_hex = digest.hexdigest().lower()
    if actual_hex == expected_hex:
        return None

    expected_norm = f"{algo}:{expected_hex}"
    actual_norm = f"{algo}:{actual_hex}"
    return PolicyFetchResult(
        outcome="hash_mismatch",
        source=source_label,
        error=(
            f"Policy hash mismatch from {source_label}: expected {expected_norm}, got {actual_norm}"
        ),
        expected_hash=expected_norm,
        raw_bytes_hash=actual_norm,
    )


# ---------------------------------------------------------------------------
# Cache entry dataclass
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    """Internal representation of a cached policy read."""

    policy: ApmPolicy
    source: str
    age_seconds: int
    stale: bool  # True if past TTL (but within MAX_STALE_TTL)
    chain_refs: list[str] = field(default_factory=list)
    fingerprint: str = ""
    raw_bytes_hash: str = ""  # "<algo>:<hex>" of leaf bytes off wire (#827)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _get_cache_dir(project_root: Path) -> Path:
    """Get the policy cache directory.

    Path-security guard (#832): the resulting path is asserted to live
    within ``project_root``.  This catches the edge case where
    ``apm_modules`` itself is a symlink that points outside the
    project root -- a configuration that, while unusual, would let
    cache reads/writes escape the project tree.
    """
    # Resolve early so candidate inherits long-name form on Windows;
    # without this, resolve() on a not-yet-existing candidate keeps
    # 8.3 short names while the base resolves to long names (#886).
    project_root = project_root.resolve()
    base = project_root / "apm_modules"
    candidate = base / POLICY_CACHE_DIR
    # Resolve both ends and assert containment under ``project_root``,
    # not under ``base`` -- otherwise a symlinked apm_modules pointing
    # outside the project would resolve through the symlink on both
    # sides and the check would silently pass.
    try:
        ensure_path_within(candidate, project_root)
    except PathTraversalError:
        raise PathTraversalError(  # noqa: B904
            f"Policy cache path '{candidate}' resolves outside "
            f"project root '{project_root}' -- refusing to read or "
            "write the cache here."
        )
    return candidate


def _cache_key(repo_ref: str) -> str:
    """Generate a deterministic cache filename from repo ref."""
    return hashlib.sha256(repo_ref.encode()).hexdigest()[:16]


def _policy_to_dict(policy: ApmPolicy) -> dict:
    """Serialize an ApmPolicy to a dict matching the YAML schema."""

    def _opt_list(val: tuple[str, ...] | None) -> list | None:
        return None if val is None else list(val)

    return {
        "name": policy.name,
        "version": policy.version,
        "enforcement": policy.enforcement,
        "fetch_failure": policy.fetch_failure,
        "cache": {"ttl": policy.cache.ttl},
        "dependencies": {
            "allow": _opt_list(policy.dependencies.allow),
            "deny": _opt_list(policy.dependencies.deny),
            "require": _opt_list(policy.dependencies.require),
            "require_resolution": policy.dependencies.require_resolution,
            "max_depth": policy.dependencies.max_depth,
        },
        "mcp": {
            "allow": _opt_list(policy.mcp.allow),
            "deny": list(policy.mcp.deny),
            "transport": {
                "allow": _opt_list(policy.mcp.transport.allow),
            },
            "self_defined": policy.mcp.self_defined,
            "trust_transitive": policy.mcp.trust_transitive,
        },
        "compilation": {
            "target": {
                "allow": _opt_list(policy.compilation.target.allow),
                "enforce": policy.compilation.target.enforce,
            },
            "strategy": {
                "enforce": policy.compilation.strategy.enforce,
            },
            "source_attribution": policy.compilation.source_attribution,
        },
        "manifest": {
            "required_fields": list(policy.manifest.required_fields),
            "scripts": policy.manifest.scripts,
            "content_types": policy.manifest.content_types,
        },
        "unmanaged_files": {
            "action": policy.unmanaged_files.action,
            "directories": list(policy.unmanaged_files.directories or ()),
        },
    }


def _serialize_policy(policy: ApmPolicy) -> str:
    """Serialize an ApmPolicy to deterministic YAML for caching."""
    return yaml.dump(
        _policy_to_dict(policy), default_flow_style=False, sort_keys=True
    )  # yaml-io-exempt


def _policy_fingerprint(serialized: str) -> str:
    """Compute a fingerprint of a serialized policy."""
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]


def _is_policy_empty(policy: ApmPolicy) -> bool:
    """Return True if a policy has no actionable restrictions.

    An 'empty' policy is syntactically valid but imposes no constraints
    beyond the permissive defaults.
    """
    return (
        not policy.dependencies.effective_deny
        and policy.dependencies.allow is None
        and not policy.dependencies.effective_require
        and not policy.mcp.deny
        and policy.mcp.allow is None
        and policy.mcp.transport.allow is None
        and policy.compilation.target.allow is None
        and not policy.manifest.required_fields
        and policy.manifest.scripts == "allow"
        and policy.manifest.content_types is None
        and policy.unmanaged_files.effective_action == "ignore"
    )


def _stale_fallback_or_error(
    cache_entry: _CacheEntry | None,
    fetch_error_msg: str,
    source_label: str,
    outcome_on_miss: str,
) -> object:  # PolicyFetchResult
    """Return stale cache if available, otherwise error with given outcome."""
    from .discovery import PolicyFetchResult

    if cache_entry is not None:
        return PolicyFetchResult(
            policy=cache_entry.policy,
            source=cache_entry.source,
            cached=True,
            cache_stale=True,
            cache_age_seconds=cache_entry.age_seconds,
            fetch_error=fetch_error_msg,
            outcome="cached_stale",
        )
    return PolicyFetchResult(
        error=fetch_error_msg,
        source=source_label,
        fetch_error=fetch_error_msg,
        outcome=outcome_on_miss,
    )


def _detect_garbage(
    content: str | None,
    identifier: str,
    source_label: str,
    cache_entry: _CacheEntry | None,
) -> object:  # PolicyFetchResult | None
    """Detect garbage responses (200 OK with non-YAML body).

    Returns a PolicyFetchResult if the content is garbage (stale fallback
    or garbage_response outcome), or None if the content looks parseable.
    """
    from .discovery import PolicyFetchResult

    if content is None:
        return None

    try:
        raw_data = yaml.safe_load(content)
    except yaml.YAMLError:
        msg = f"Response from {identifier} is not valid YAML"
        if cache_entry is not None:
            return PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_stale=True,
                cache_age_seconds=cache_entry.age_seconds,
                fetch_error=msg,
                outcome="cached_stale",
            )
        return PolicyFetchResult(
            error=msg + " (possible captive portal or redirect)",
            source=source_label,
            fetch_error=msg,
            outcome="garbage_response",
        )

    if raw_data is not None and not isinstance(raw_data, dict):
        msg = f"Response from {identifier} is not a YAML mapping"
        if cache_entry is not None:
            return PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_stale=True,
                cache_age_seconds=cache_entry.age_seconds,
                fetch_error=msg,
                outcome="cached_stale",
            )
        return PolicyFetchResult(
            error=msg,
            source=source_label,
            fetch_error=msg,
            outcome="garbage_response",
        )

    return None  # Not garbage -- proceed with normal parsing


def _read_cache_entry(
    repo_ref: str,
    project_root: Path,
    ttl: int = DEFAULT_CACHE_TTL,
    *,
    expected_hash: str | None = None,
) -> _CacheEntry | None:
    """Read cache entry with stale-awareness.

    Returns:
    * ``_CacheEntry(stale=False)`` -- within TTL, ready for immediate use
    * ``_CacheEntry(stale=True)``  -- past TTL but within MAX_STALE_TTL
    * ``None``                     -- no cache file, corrupt, past MAX_STALE_TTL,
                                       or pin verification failure (#827).

    When *expected_hash* is provided the cached ``raw_bytes_hash`` is
    compared against it; a mismatch invalidates the cache entry so the
    caller falls through to a fresh fetch where the pin can be verified
    against authoritative bytes off the wire.
    """
    cache_dir = _get_cache_dir(project_root)
    key = _cache_key(repo_ref)
    policy_file = cache_dir / f"{key}.yml"
    meta_file = cache_dir / f"{key}.meta.json"

    if not policy_file.exists() or not meta_file.exists():
        return None

    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))

        # Schema version check -- auto-invalidate on format change
        if meta.get("schema_version") != CACHE_SCHEMA_VERSION:
            return None

        cached_at = meta.get("cached_at", 0)
        age = int(time.time() - cached_at)

        if age > MAX_STALE_TTL:
            return None  # Past MAX_STALE_TTL, unusable

        raw_bytes_hash = meta.get("raw_bytes_hash", "") or ""

        # Pin verification (#827): if the project pinned a hash and the
        # cache was written without one (legacy entry) or with a different
        # one, ignore the cache so the fetcher can verify the pin against
        # fresh authoritative bytes.
        if expected_hash is not None:
            try:
                exp_algo, exp_hex = _split_hash_pin(expected_hash)
                expected_norm = f"{exp_algo}:{exp_hex}"
            except ProjectPolicyConfigError:
                return None
            if raw_bytes_hash.lower() != expected_norm:
                return None

        policy, _warnings = load_policy(policy_file)

        # Determine source label
        if repo_ref.startswith("http://") or repo_ref.startswith("https://"):
            source = f"url:{repo_ref}"
        else:
            source = f"org:{repo_ref}"

        return _CacheEntry(
            policy=policy,
            source=source,
            age_seconds=age,
            stale=age > ttl,
            chain_refs=meta.get("chain_refs", [repo_ref]),
            fingerprint=meta.get("fingerprint", ""),
            raw_bytes_hash=raw_bytes_hash,
        )
    except Exception:
        return None


def _read_cache(
    repo_ref: str,
    project_root: Path,
    ttl: int = DEFAULT_CACHE_TTL,
) -> object:  # PolicyFetchResult | None
    """Read policy from cache if still valid (within TTL).

    Legacy wrapper around ``_read_cache_entry`` for backward compatibility.
    Returns None if cache miss, expired, or past MAX_STALE_TTL.
    """
    from .discovery import PolicyFetchResult

    entry = _read_cache_entry(repo_ref, project_root, ttl=ttl)
    if entry is None or entry.stale:
        return None
    outcome = "empty" if _is_policy_empty(entry.policy) else "found"
    return PolicyFetchResult(
        policy=entry.policy,
        source=entry.source,
        cached=True,
        cache_age_seconds=entry.age_seconds,
        outcome=outcome,
    )


def _write_cache(
    repo_ref: str,
    policy: ApmPolicy,
    project_root: Path,
    *,
    chain_refs: list[str] | None = None,
    raw_bytes_hash: str | None = None,
) -> None:
    """Write merged effective policy and metadata to cache atomically.

    Uses temp file + ``os.replace()`` to prevent torn writes from parallel
    installs.  Both the policy file and metadata sidecar are written
    atomically and independently.

    The optional ``raw_bytes_hash`` (canonical ``"<algo>:<hex>"``) is the
    digest of the leaf bytes off the wire and is persisted to the meta
    sidecar so subsequent cached reads can verify against the project's
    pin without re-fetching (#827).
    """
    cache_dir = _get_cache_dir(project_root)
    cache_dir.mkdir(parents=True, exist_ok=True)

    key = _cache_key(repo_ref)
    policy_file = cache_dir / f"{key}.yml"
    meta_file = cache_dir / f"{key}.meta.json"

    serialized = _serialize_policy(policy)
    fingerprint = _policy_fingerprint(serialized)

    # Unique tmp suffix to avoid collisions from parallel writers
    uid = f"{os.getpid()}.{threading.get_ident()}"

    # Atomic write: policy file
    tmp_policy = cache_dir / f"{key}.{uid}.yml.tmp"
    try:
        tmp_policy.write_text(serialized, encoding="utf-8")
        os.replace(str(tmp_policy), str(policy_file))
    except OSError:
        # Best-effort cleanup
        try:  # noqa: SIM105
            tmp_policy.unlink(missing_ok=True)
        except OSError:
            pass
        return

    # Atomic write: metadata sidecar
    meta = {
        "repo_ref": repo_ref,
        "cached_at": time.time(),
        "chain_refs": chain_refs if chain_refs is not None else [repo_ref],
        "schema_version": CACHE_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "raw_bytes_hash": raw_bytes_hash or "",
    }
    tmp_meta = cache_dir / f"{key}.{uid}.meta.json.tmp"
    try:
        tmp_meta.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(str(tmp_meta), str(meta_file))
    except OSError:
        try:  # noqa: SIM105
            tmp_meta.unlink(missing_ok=True)
        except OSError:
            pass
