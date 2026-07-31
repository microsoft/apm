"""Auto-discover and fetch org-level apm-policy.yml files.

Discovery flow:
1. Extract org from git remote (github.com/contoso/my-project -> "contoso")
2. Determine host profile (default or ado) to select candidate repos
3. Try candidate repos in precedence order (.github > .apm > _apm)
4. Fetch apm-policy.yml via GitHub Contents API or ADO Items API
5. Resolve inheritance chain via resolve_policy_chain
6. Cache the **merged effective policy** with chain metadata
7. Parse and return ApmPolicy

Candidate repo precedence:
- .github  -- GitHub convention (skipped on ADO)
- .apm     -- cross-platform convention (skipped on ADO)
- _apm     -- universal fallback (valid on every git host)

Supports:
- GitHub.com and GitHub Enterprise (*.ghe.com)
- Azure DevOps (dev.azure.com, *.visualstudio.com)
- Manual override via --policy <path|url>
- Cache with TTL (default 1 hour), stale fallback up to MAX_STALE_TTL
- Atomic cache writes (temp file + os.replace)
- Garbage-response detection (200 OK with non-YAML body)
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading  # noqa: F401
import time  # noqa: F401 -- seam: _discovery_cache.py uses _d.time.time() to allow test patching
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse  # noqa: F401 -- re-exported seam for test patches

import requests

from ..utils.github_host import (
    is_azure_devops_hostname,
)
from ._discovery_ado import (
    _fetch_ado_contents as _fetch_ado_contents,
)
from ._discovery_ado import (
    _fetch_from_ado_repo as _fetch_from_ado_repo,
)
from ._discovery_ado import (
    _get_token_for_host as _get_token_for_host,
)
from ._discovery_ado import (
    _is_github_host as _is_github_host,
)
from ._discovery_ado import (
    _load_from_file as _load_from_file,
)
from ._discovery_ado import (
    _parse_remote_url as _parse_remote_url,
)
from ._discovery_ado import (
    _parse_scheme_remote_url as _parse_scheme_remote_url,
)
from ._discovery_cache import (
    CACHE_SCHEMA_VERSION as CACHE_SCHEMA_VERSION,
)
from ._discovery_cache import (
    DEFAULT_CACHE_TTL as DEFAULT_CACHE_TTL,
)
from ._discovery_cache import (
    MAX_STALE_TTL as MAX_STALE_TTL,
)
from ._discovery_cache import (
    POLICY_CACHE_DIR as POLICY_CACHE_DIR,
)
from ._discovery_cache import (
    _cache_entry_files_exist as _cache_entry_files_exist,
)
from ._discovery_cache import (
    _cache_key as _cache_key,
)
from ._discovery_cache import (
    _cache_only_policy_result as _cache_only_policy_result,
)
from ._discovery_cache import (
    _CacheEntry as _CacheEntry,
)
from ._discovery_cache import (
    _compute_hash_normalized as _compute_hash_normalized,
)
from ._discovery_cache import (
    _detect_garbage as _detect_garbage,
)
from ._discovery_cache import (
    _get_cache_dir as _get_cache_dir,
)
from ._discovery_cache import (
    _is_policy_empty as _is_policy_empty,
)
from ._discovery_cache import (
    _policy_fingerprint as _policy_fingerprint,
)
from ._discovery_cache import (
    _policy_to_dict as _policy_to_dict,
)
from ._discovery_cache import (
    _read_cache as _read_cache,
)
from ._discovery_cache import (
    _read_cache_entry as _read_cache_entry,
)
from ._discovery_cache import (
    _serialize_policy as _serialize_policy,
)
from ._discovery_cache import (
    _split_hash_pin as _split_hash_pin,
)
from ._discovery_cache import (
    _stale_fallback_or_error as _stale_fallback_or_error,
)
from ._discovery_cache import (
    _unverifiable_cache_pin as _unverifiable_cache_pin,
)
from ._discovery_cache import (
    _verify_hash_pin as _verify_hash_pin,
)
from ._discovery_cache import (
    _write_cache as _write_cache,
)
from ._discovery_cache import (
    policy_cache_available as policy_cache_available,
)
from ._discovery_chain import (
    _derive_leaf_host as _derive_leaf_host,
)
from ._discovery_chain import (
    _extract_extends_host as _extract_extends_host,
)
from ._discovery_chain import (
    _fetch_chain_parent as _fetch_chain_parent,
)
from ._discovery_chain import (
    _resolve_ado_parent_ref as _resolve_ado_parent_ref,
)
from ._discovery_chain import (
    _resolve_and_persist_chain as _resolve_and_persist_chain,
)
from ._discovery_chain import (
    _strip_source_prefix as _strip_source_prefix,
)
from ._discovery_chain import (
    _validate_extends_host as _validate_extends_host,
)
from ._discovery_github import (
    _call_github_api as _call_github_api,
)
from ._discovery_github import (
    _decode_github_content as _decode_github_content,
)
from ._discovery_github import (
    _fetch_github_contents as _fetch_github_contents,
)
from ._discovery_github import (
    _parse_github_repo_ref as _parse_github_repo_ref,
)
from .parser import PolicyValidationError, load_policy
from .project_config import (
    ProjectPolicyConfigError,
    read_project_policy_hash_pin,
)
from .schema import ApmPolicy

logger = logging.getLogger(__name__)


# Candidate repo names in precedence order (first valid policy wins).
_DEFAULT_POLICY_REPOS: tuple[str, ...] = (".github-private", ".github", ".apm", "_apm")
_ADO_POLICY_REPOS: tuple[str, ...] = ("_apm",)

# ADO project name for the policy repo (ADO requires a project container).
ADO_POLICY_PROJECT = "_apm"


def _policy_repo_candidates(host: str) -> tuple[str, ...]:
    """Return candidate policy repo names for *host* in precedence order.

    ADO hosts cannot have repo names starting/ending with ``.``, so only
    ``_apm`` is valid.  All other hosts try the full cascade.
    """
    if is_azure_devops_hostname(host):
        return _ADO_POLICY_REPOS
    return _DEFAULT_POLICY_REPOS


@dataclass
class PolicyFetchResult:
    """Result of a policy fetch attempt.

    The ``outcome`` field discriminates the 9 discovery outcomes defined in
    the plan (section B):

    * ``found``               -- valid policy, enforce per ``enforcement``
    * ``absent``              -- no policy published (404 / empty repo)
    * ``cached_stale``        -- served from cache past TTL on refresh failure
    * ``cache_miss_fetch_fail`` -- no cache, fetch failed
    * ``malformed``           -- YAML valid but schema invalid (fail-closed)
    * ``disabled``            -- ``--no-policy`` / ``APM_POLICY_DISABLE=1``
    * ``garbage_response``    -- 200 OK but body is not valid YAML
    * ``no_git_remote``       -- cannot determine org from git remote
    * ``empty``               -- valid policy with no actionable rules
    * ``hash_mismatch``       -- ``policy.hash`` pin in apm.yml does not match
                                 the fetched policy bytes (always fail-closed)
    """

    policy: ApmPolicy | None = None
    source: str = ""  # "org:contoso/.github", "file:/path", "url:https://..."
    cached: bool = False  # True if served from cache
    error: str | None = None  # Error message if fetch failed

    # -- Outcome-matrix fields (W1-cache-redesign) --
    cache_age_seconds: int | None = None  # Age of cache entry in seconds
    cache_stale: bool = False  # True if cache was served past TTL
    fetch_error: str | None = None  # Network/parse error on refresh attempt
    outcome: str = ""  # See docstring for valid values

    # -- Hash-pin fields (#827 supply-chain hardening) --
    # raw_bytes_hash is the digest of the leaf policy bytes off the wire,
    # in canonical "<algo>:<hex>" form. Persisted to the cache so subsequent
    # cached reads can verify against the project's pin without re-fetching.
    raw_bytes_hash: str | None = None
    expected_hash: str | None = None  # The pin that was checked, if any

    # -- Warnings (informational messages from discovery) --
    warnings: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []

    @property
    def found(self) -> bool:
        return self.policy is not None


def discover_policy_with_chain(
    project_root: Path,
    *,
    policy_override: str | None = None,
    no_cache: bool = False,
    cache_only: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Discover policy with full inheritance chain resolution.

    This is the **shared entry point** for all command sites that need
    chain-aware policy discovery (gate phase, ``--mcp`` preflight,
    ``--dry-run`` preflight).  It ensures every path resolves the same
    merged effective policy with real ``chain_refs``.

    Parameters
    ----------
    project_root:
        Project root directory (used for git-remote org extraction and cache).
    policy_override:
        Optional override for the policy source (file path, URL, or org ref).
    no_cache:
        Skip the cache and always fetch fresh from the network.
    cache_only:
        Only serve from cache; never fetch from the network.
    expected_hash:
        Optional pin in ``"<algo>:<hex>"`` form (sourced from
        ``policy.hash`` in the project's ``apm.yml``). When set, the
        digest of the leaf policy bytes must match exactly; otherwise the
        result outcome is set to ``"hash_mismatch"`` and ``policy`` is
        cleared. The pin applies only to the **leaf** -- parent policies
        in an ``extends:`` chain are the leaf author's responsibility.

    Notes
    -----
    The escape hatch (``--no-policy`` flag, ``APM_POLICY_DISABLE=1``
    env var) is enforced by the **callers** (the install pipeline gate
    and the preflight helpers in ``install_preflight``) **before** this
    function is invoked, so neither needs a ``no_policy`` parameter
    here.  The env-var check below remains as a defence-in-depth so
    third-party callers cannot accidentally bypass the disable switch.

    Returns
    -------
    PolicyFetchResult
        With merged effective policy and real chain_refs when inheritance
        is present.  Outcome follows the 9-outcome matrix (section B).
    """
    # -- Escape hatch (defence-in-depth) -------------------------------
    if os.environ.get("APM_POLICY_DISABLE") == "1":
        return PolicyFetchResult(outcome="disabled")

    # -- Resolve project-side hash pin (#827) --------------------------
    # Must happen before cache_only shortcut so an invalid pin triggers
    # hash_mismatch even in offline / cache-only mode.
    if expected_hash is None:
        try:
            pin = read_project_policy_hash_pin(project_root)
        except ProjectPolicyConfigError as exc:
            return PolicyFetchResult(
                outcome="hash_mismatch",
                source="apm.yml",
                error=f"Invalid policy.hash in apm.yml: {exc}",
            )
        if pin is not None:
            expected_hash = pin.normalized

    # -- Local file override + cache_only: fetch file, then resolve chain offline
    if policy_override:
        local_path = Path(policy_override)
        if local_path.exists() and local_path.is_file():
            fetch_result = discover_policy(
                project_root,
                policy_override=policy_override,
                no_cache=True,
                expected_hash=expected_hash,
            )
            if fetch_result.policy is not None and fetch_result.policy.extends is not None:
                _resolve_and_persist_chain(
                    fetch_result,
                    project_root,
                    no_cache=no_cache,
                    cache_only=cache_only,
                )
            return fetch_result

    # -- Cache-only mode ------------------------------------------------
    # Short-circuit: if no cache files exist at all, we cannot serve from
    # cache and the git subprocess call in _auto_discover is unnecessary.
    # Return a result that still honours project-side policy settings
    # (fetch_failure_default and expected_hash) without making any
    # network or subprocess calls.
    if cache_only and not (policy_override and Path(policy_override).is_file()):
        if not _any_policy_cache_exists(project_root):
            if expected_hash is not None:
                return PolicyFetchResult(
                    source="",
                    outcome="hash_mismatch",
                    error="Policy hash pin cannot be verified without cached policy bytes",
                    expected_hash=expected_hash,
                )
            return PolicyFetchResult(outcome="absent")
    if cache_only:
        return discover_policy(
            project_root,
            policy_override=policy_override,
            no_cache=False,
            cache_only=True,
            expected_hash=expected_hash,
        )

    # -- Base discovery ------------------------------------------------
    fetch_result = discover_policy(
        project_root,
        policy_override=policy_override,
        no_cache=no_cache,
        expected_hash=expected_hash,
    )

    # -- Chain resolution if leaf has extends: -------------------------
    if (
        fetch_result.policy is not None
        and fetch_result.policy.extends is not None
        and not fetch_result.cached  # Don't re-resolve if served from cache
    ):
        _resolve_and_persist_chain(
            fetch_result,
            project_root,
            no_cache=no_cache,
            cache_only=cache_only,
        )

    return fetch_result


def discover_policy(
    project_root: Path,
    *,
    policy_override: str | None = None,
    no_cache: bool = False,
    cache_only: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Discover and load the applicable policy for a project.

    Resolution order:
    1. If policy_override is a local file path -> load from file
    2. If policy_override is an https:// URL -> fetch from URL (http:// is rejected)
    3. If policy_override is "org" -> auto-discover from project's git remote
    4. If policy_override is "owner/repo" -> fetch from that repo via GitHub Contents API
    5. If policy_override is None -> auto-discover from project's git remote

    The optional ``expected_hash`` (``"<algo>:<hex>"``) pins the leaf
    policy bytes; mismatches return ``outcome="hash_mismatch"`` (fail-closed).
    """
    if policy_override:
        path = Path(policy_override)
        if path.exists() and path.is_file():
            return _load_from_file(path, expected_hash=expected_hash)
        if policy_override.startswith("http://"):
            return PolicyFetchResult(
                error="Refusing plaintext http:// policy URL -- use https://",
                source=f"url:{policy_override}",
            )
        if policy_override.startswith("https://"):
            return _fetch_from_url(
                policy_override,
                project_root,
                no_cache=no_cache,
                expected_hash=expected_hash,
            )
        if policy_override != "org":
            # Try as owner/repo reference
            return _fetch_from_repo(
                policy_override,
                project_root,
                no_cache=no_cache,
                cache_only=cache_only,
                expected_hash=expected_hash,
            )

    # Auto-discover from git remote
    return _auto_discover(
        project_root,
        no_cache=no_cache,
        cache_only=cache_only,
        expected_hash=expected_hash,
    )


def _auto_discover(
    project_root: Path,
    *,
    no_cache: bool = False,
    cache_only: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Auto-discover policy by cascading through candidate repos.

    Tries .github-private > .github > .apm > _apm in order; returns the first
    match or outcome="absent" if all are absent.
    """
    org_and_host = _extract_org_from_git_remote(project_root)
    if org_and_host is None:
        return PolicyFetchResult(
            error="Could not determine org from git remote",
            outcome="no_git_remote",
        )

    org, host = org_and_host
    candidates = _policy_repo_candidates(host)
    is_ado = is_azure_devops_hostname(host)

    # cache_only: serve from cache for the first candidate that has one
    if cache_only:
        for candidate_repo in candidates:
            repo_ref = f"{org}/{candidate_repo}"
            if host and host != "github.com":
                repo_ref = f"{host}/{repo_ref}"
            result = _cache_only_policy_result(repo_ref, project_root)
            if result.outcome != "not_found":
                return result
        return PolicyFetchResult(
            error="No cached policy available (cache_only=True)",
            outcome="absent",
        )

    for candidate_repo in candidates:
        logger.debug("Trying org policy repo candidate %s on host %s", candidate_repo, host)
        if is_ado:
            result = _fetch_from_ado_repo(
                org=org,
                project=ADO_POLICY_PROJECT,
                repo=candidate_repo,
                host=host,
                project_root=project_root,
                no_cache=no_cache,
                expected_hash=expected_hash,
            )
        else:
            repo_ref = f"{org}/{candidate_repo}"
            if host and host != "github.com":
                repo_ref = f"{host}/{repo_ref}"
            result = _fetch_from_repo(
                repo_ref, project_root, no_cache=no_cache, expected_hash=expected_hash
            )

        # 404 / absent -> try the next candidate
        if result.outcome == "absent":
            logger.debug(
                "Policy repo candidate %s absent on host %s; trying next candidate",
                candidate_repo,
                host,
            )
            continue

        # Any other outcome (found, error, malformed, etc.) -> return immediately
        return result

    # All candidates exhausted: no policy published anywhere.
    return PolicyFetchResult(
        error=None,
        outcome="absent",
    )


def _any_policy_cache_exists(project_root: Path) -> bool:
    """Return True if any cached policy entry exists for this project.

    Scans ``apm_modules/.policy-cache/`` for ``*.yml`` files with a
    matching ``*.meta.json`` sidecar -- the pair that the cache writer
    always creates.  This check requires no git subprocess call and is
    safe to use in air-gapped / offline environments.
    """
    cache_dir = _get_cache_dir(project_root)
    if not cache_dir.is_dir():
        return False
    return any((cache_dir / f"{p.stem}.meta.json").is_file() for p in cache_dir.glob("*.yml"))


def _extract_org_from_git_remote(
    project_root: Path,
) -> tuple[str, str] | None:
    """Extract (org, host) from git remote origin URL.

    Handles:
    - https://github.com/contoso/my-project.git -> ("contoso", "github.com")
    - git@github.com:contoso/my-project.git -> ("contoso", "github.com")
    - https://github.example.com/contoso/my-project.git -> ("contoso", "github.example.com")
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=project_root,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        return _parse_remote_url(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _fetch_from_url(
    url: str,
    project_root: Path,
    *,
    no_cache: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Fetch policy YAML from a direct URL."""
    source_label = f"url:{url}"
    cache_entry: _CacheEntry | None = None

    if not no_cache:
        cache_entry = _read_cache_entry(url, project_root, expected_hash=expected_hash)
        if cache_entry is not None and not cache_entry.stale:
            outcome = "empty" if _is_policy_empty(cache_entry.policy) else "found"
            return PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_age_seconds=cache_entry.age_seconds,
                outcome=outcome,
                raw_bytes_hash=cache_entry.raw_bytes_hash or None,
                expected_hash=expected_hash,
                warnings=cache_entry.warnings,
            )

    fetch_error: str | None = None
    content: str | None = None

    try:
        resp = requests.get(url, timeout=10, allow_redirects=False)
        if resp.status_code == 404:
            return PolicyFetchResult(
                source=source_label,
                error="404: Policy file not found",
                outcome="absent",
            )
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("Location", "<no Location header>")
            fetch_error = f"Refusing HTTP redirect ({resp.status_code}) from {url} to {location}"
        elif resp.status_code != 200:
            fetch_error = f"HTTP {resp.status_code} fetching {url}"
        else:
            content = resp.text
    except requests.exceptions.Timeout:
        fetch_error = f"Timeout fetching {url}"
    except requests.exceptions.ConnectionError:
        fetch_error = f"Connection error fetching {url}"
    except Exception as e:
        fetch_error = f"Error fetching {url}: {e}"

    if fetch_error:
        return _stale_fallback_or_error(
            cache_entry, fetch_error, source_label, "cache_miss_fetch_fail"
        )

    garbage_result = _detect_garbage(content, url, source_label, cache_entry)
    if garbage_result is not None:
        return garbage_result

    mismatch = _verify_hash_pin(content, expected_hash, source_label)
    if mismatch is not None:
        return mismatch

    try:
        policy, warnings = load_policy(content)
    except PolicyValidationError as e:
        return PolicyFetchResult(
            error=f"Invalid policy from {url}: {e}",
            source=source_label,
            outcome="malformed",
            warnings=e.warnings,
        )

    chain_refs = [url]
    actual_hash = _compute_hash_normalized(content, expected_hash)
    # Defer cache write for policies with extends: -- chain resolver writes merged cache.
    if not policy.extends:
        _write_cache(
            url,
            policy,
            project_root,
            chain_refs=chain_refs,
            raw_bytes_hash=actual_hash,
            warnings=warnings,
        )
    outcome = "empty" if _is_policy_empty(policy) else "found"
    return PolicyFetchResult(
        policy=policy,
        source=source_label,
        outcome=outcome,
        raw_bytes_hash=actual_hash,
        expected_hash=expected_hash,
        warnings=warnings,
    )


def _fetch_from_repo_cache_only(
    repo_ref: str,
    project_root: Path,
    cache_entry: object,  # _CacheEntry | None
    expected_hash: str | None,
    source_label: str,
) -> PolicyFetchResult:
    """Serve a repo policy from cache without hitting the network."""
    if expected_hash is not None and _unverifiable_cache_pin(repo_ref, project_root, expected_hash):
        return PolicyFetchResult(
            source=source_label,
            outcome="hash_mismatch",
            error=f"Cached policy hash does not match pin {expected_hash}",
        )
    entry = cache_entry
    if entry is None:
        entry = _read_cache_entry(repo_ref, project_root, expected_hash=expected_hash)
    if entry is None:
        # Fail-closed when a hash pin is required but no cache entry to verify against.
        if expected_hash is not None:
            return PolicyFetchResult(
                source=source_label,
                outcome="hash_mismatch",
                error=f"No cached policy to verify against pin {expected_hash}",
            )
        return PolicyFetchResult(source=source_label, outcome="absent")
    return PolicyFetchResult(
        policy=entry.policy,
        source=entry.source,
        cached=True,
        cache_stale=True,
        cache_age_seconds=entry.age_seconds,
        outcome="cached_stale",
        warnings=entry.warnings,
    )


def _fetch_from_repo(
    repo_ref: str,
    project_root: Path,
    *,
    no_cache: bool = False,
    cache_only: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Fetch apm-policy.yml from a GitHub repo via Contents API.

    repo_ref format: "owner/.github" or "host/owner/.github"
    """
    source_label = f"org:{repo_ref}"
    cache_entry: _CacheEntry | None = None

    if not no_cache:
        cache_entry = _read_cache_entry(repo_ref, project_root, expected_hash=expected_hash)
        if cache_entry is not None and not cache_entry.stale:
            outcome = "empty" if _is_policy_empty(cache_entry.policy) else "found"
            return PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_age_seconds=cache_entry.age_seconds,
                outcome=outcome,
                raw_bytes_hash=cache_entry.raw_bytes_hash or None,
                expected_hash=expected_hash,
                warnings=cache_entry.warnings,
            )

    if cache_only:
        return _fetch_from_repo_cache_only(
            repo_ref, project_root, cache_entry, expected_hash, source_label
        )

    content, error = _fetch_github_contents(repo_ref, "apm-policy.yml")

    if error or content is None:
        if error and "404" not in error:
            return _stale_fallback_or_error(
                cache_entry, error, source_label, "cache_miss_fetch_fail"
            )
        return PolicyFetchResult(source=source_label, outcome="absent")

    garbage_result = _detect_garbage(content, repo_ref, source_label, cache_entry)
    if garbage_result is not None:
        return garbage_result

    mismatch = _verify_hash_pin(content, expected_hash, source_label)
    if mismatch is not None:
        return mismatch

    try:
        policy, warnings = load_policy(content)
    except PolicyValidationError as e:
        return PolicyFetchResult(
            error=f"Invalid policy in {repo_ref}: {e}",
            source=source_label,
            outcome="malformed",
            warnings=e.warnings,
        )

    chain_refs = [repo_ref]
    actual_hash = _compute_hash_normalized(content, expected_hash)
    # Defer cache write for policies with extends: -- chain resolver writes merged cache.
    if not policy.extends:
        _write_cache(
            repo_ref,
            policy,
            project_root,
            chain_refs=chain_refs,
            raw_bytes_hash=actual_hash,
            warnings=warnings,
        )
    outcome = "empty" if _is_policy_empty(policy) else "found"
    return PolicyFetchResult(
        policy=policy,
        source=source_label,
        outcome=outcome,
        raw_bytes_hash=actual_hash,
        expected_hash=expected_hash,
        warnings=warnings,
    )
