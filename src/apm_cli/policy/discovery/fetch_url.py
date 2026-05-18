"""Auto-discover and fetch org-level apm-policy.yml files.

Discovery flow:
1. Extract org from git remote (github.com/contoso/my-project -> "contoso")
2. Fetch <org>/.github/apm-policy.yml via GitHub API (Contents API)
3. Resolve inheritance chain via resolve_policy_chain
4. Cache the **merged effective policy** with chain metadata
5. Parse and return ApmPolicy

Supports:
- GitHub.com and GitHub Enterprise (*.ghe.com)
- Manual override via --policy <path|url>
- Cache with TTL (default 1 hour), stale fallback up to MAX_STALE_TTL
- Atomic cache writes (temp file + os.replace)
- Garbage-response detection (200 OK with non-YAML body)
"""

from __future__ import annotations

import base64
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

from ..parser import PolicyValidationError, load_policy
from .cache import (
    _detect_garbage,
    _is_policy_empty,
    _read_cache_entry,
    _stale_fallback_or_error,
    _write_cache,
)
from .class_ import PolicyFetchResult, _CacheEntry
from .github_token import _get_token_for_host
from .hash_verify import _compute_hash_normalized, _verify_hash_pin

logger = logging.getLogger(__name__)
POLICY_CACHE_DIR = ".policy-cache"


def _pkg():
    return sys.modules[__package__]


DEFAULT_CACHE_TTL = 3600  # 1 hour
MAX_STALE_TTL = 7 * 24 * 3600  # 7 days -- stale cache usable on refresh failure
CACHE_SCHEMA_VERSION = "3"  # Bump when cache format changes to auto-invalidate


@dataclass(frozen=True, slots=True)
class ProcessPolicyOpts:
    """Options for _process_fetched_policy helper."""

    content: str
    ref: str
    source_label: str
    cache_entry: _CacheEntry | None
    expected_hash: str | None
    project_root: Path


def _fetch_from_url(
    url: str,
    project_root: Path,
    *,
    no_cache: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Fetch policy YAML from a direct URL."""
    source_label = f"url:{url}"

    # Try cache first
    if not no_cache:
        cache_entry = _pkg()._read_cache_entry(url, project_root, expected_hash=expected_hash)
        if cache_entry is not None and not cache_entry.stale:
            outcome = "empty" if _pkg()._is_policy_empty(cache_entry.policy) else "found"
            return PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_age_seconds=cache_entry.age_seconds,
                outcome=outcome,
                raw_bytes_hash=cache_entry.raw_bytes_hash or None,
                expected_hash=expected_hash,
            )
    else:
        cache_entry = None

    # Fetch from URL
    content, fetch_error = _fetch_url_content(url)
    if fetch_error:
        return _pkg()._stale_fallback_or_error(
            cache_entry, fetch_error, source_label, "cache_miss_fetch_fail"
        )
    if content is None:
        return PolicyFetchResult(
            source=source_label,
            error="404: Policy file not found",
            outcome="absent",
        )

    # Verify and parse
    return _process_fetched_policy(
        ProcessPolicyOpts(
            content=content,
            ref=url,
            source_label=source_label,
            cache_entry=cache_entry,
            expected_hash=expected_hash,
            project_root=project_root,
        )
    )


def _fetch_url_content(url: str) -> tuple[str | None, str | None]:
    """Fetch content from URL; returns (content, error)."""
    try:
        resp = _pkg().requests.get(url, timeout=10, allow_redirects=False)
        if resp.status_code == 404:
            return None, None
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("Location", "<no Location header>")
            return None, f"Refusing HTTP redirect ({resp.status_code}) from {url} to {location}"
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code} fetching {url}"
        return resp.text, None
    except _pkg().requests.exceptions.Timeout:
        return None, f"Timeout fetching {url}"
    except _pkg().requests.exceptions.ConnectionError:
        return None, f"Connection error fetching {url}"
    except Exception as e:
        return None, f"Error fetching {url}: {e}"


def _process_fetched_policy(opts: ProcessPolicyOpts) -> PolicyFetchResult:
    """Verify and parse fetched policy content."""
    # Garbage-response detection
    garbage_result = _pkg()._detect_garbage(
        opts.content, opts.ref, opts.source_label, opts.cache_entry
    )
    if garbage_result is not None:
        return garbage_result

    # Hash pin verification
    mismatch = _pkg()._verify_hash_pin(opts.content, opts.expected_hash, opts.source_label)
    if mismatch is not None:
        return mismatch

    # Parse policy
    try:
        policy, _warnings = load_policy(opts.content)
    except PolicyValidationError as e:
        return PolicyFetchResult(
            error=f"Invalid policy from {opts.ref}: {e}",
            source=opts.source_label,
            outcome="malformed",
        )

    # Cache and return
    chain_refs = [opts.ref]
    actual_hash = _pkg()._compute_hash_normalized(opts.content, opts.expected_hash)
    _pkg()._write_cache(
        opts.ref,
        policy,
        opts.project_root,
        chain_refs=chain_refs,
        raw_bytes_hash=actual_hash,
    )
    outcome = "empty" if _pkg()._is_policy_empty(policy) else "found"
    return PolicyFetchResult(
        policy=policy,
        source=opts.source_label,
        outcome=outcome,
        raw_bytes_hash=actual_hash,
        expected_hash=opts.expected_hash,
    )


def _fetch_from_repo(
    repo_ref: str,
    project_root: Path,
    *,
    no_cache: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Fetch apm-policy.yml from a GitHub repo via Contents API.

    repo_ref format: "owner/.github" or "host/owner/.github"
    """
    source_label = f"org:{repo_ref}"

    # Try cache first
    if not no_cache:
        cache_entry = _pkg()._read_cache_entry(repo_ref, project_root, expected_hash=expected_hash)
        if cache_entry is not None and not cache_entry.stale:
            outcome = "empty" if _pkg()._is_policy_empty(cache_entry.policy) else "found"
            return PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_age_seconds=cache_entry.age_seconds,
                outcome=outcome,
                raw_bytes_hash=cache_entry.raw_bytes_hash or None,
                expected_hash=expected_hash,
            )
    else:
        cache_entry = None

    # Fetch from GitHub
    content, error = _pkg()._fetch_github_contents(repo_ref, "apm-policy.yml")

    if error:
        if "404" in error:
            return PolicyFetchResult(source=source_label, outcome="absent")
        return _stale_fallback_or_error(cache_entry, error, source_label, "cache_miss_fetch_fail")

    if content is None:
        return PolicyFetchResult(source=source_label, outcome="absent")

    # Verify and parse
    return _process_fetched_policy(
        ProcessPolicyOpts(
            content=content,
            ref=repo_ref,
            source_label=source_label,
            cache_entry=cache_entry,
            expected_hash=expected_hash,
            project_root=project_root,
        )
    )


def _fetch_github_contents(
    repo_ref: str,
    file_path: str,
) -> tuple[str | None, str | None]:
    """Fetch file contents from GitHub API.

    Returns (content_string, error_string). One will be None.
    """
    # Parse and build API URL
    api_url, error = _build_github_api_url(repo_ref, file_path)
    if error:
        return None, error

    # Fetch from API
    return _fetch_github_api(api_url, repo_ref)


def _build_github_api_url(repo_ref: str, file_path: str) -> tuple[str | None, str | None]:
    """Build GitHub API URL from repo ref; returns (url, error)."""
    parts = repo_ref.split("/")
    if len(parts) == 2:
        host = "github.com"
        owner, repo = parts
    elif len(parts) >= 3:
        host = parts[0]
        owner = parts[1]
        repo = "/".join(parts[2:])
    else:
        return None, f"Invalid repo reference: {repo_ref}"

    if host == "github.com":
        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
    else:
        api_url = f"https://{host}/api/v3/repos/{owner}/{repo}/contents/{file_path}"

    return api_url, None


def _fetch_github_api(api_url: str, repo_ref: str) -> tuple[str | None, str | None]:
    """Fetch and decode content from GitHub API; returns (content, error)."""
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = _pkg()._get_token_for_host(_extract_host(repo_ref))
    if token:
        headers["Authorization"] = f"token {token}"

    try:
        resp = _pkg().requests.get(api_url, headers=headers, timeout=10, allow_redirects=False)
        return _handle_github_response(resp, api_url, repo_ref)
    except _pkg().requests.exceptions.Timeout:
        return None, f"Timeout fetching policy from {repo_ref}"
    except _pkg().requests.exceptions.ConnectionError:
        return None, f"Connection error fetching policy from {repo_ref}"
    except Exception as e:
        return None, f"Error fetching policy from {repo_ref}: {e}"


def _extract_host(repo_ref: str) -> str:
    """Extract host from repo_ref."""
    parts = repo_ref.split("/")
    return parts[0] if len(parts) >= 3 else "github.com"


def _handle_github_response(
    resp,
    api_url: str,
    repo_ref: str,
) -> tuple[str | None, str | None]:
    """Handle GitHub API response; returns (content, error)."""
    if resp.status_code == 404:
        return None, "404: Policy file not found"
    if resp.status_code == 403:
        return None, f"403: Access denied to {repo_ref}"
    if 300 <= resp.status_code < 400:
        location = resp.headers.get("Location", "<no Location header>")
        return None, f"Refusing HTTP redirect ({resp.status_code}) from {api_url} to {location}"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code} fetching policy from {repo_ref}"

    data = resp.json()
    if data.get("encoding") == "base64" and data.get("content"):
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, None
    if data.get("content"):
        return data["content"], None
    return None, f"Unexpected response format from {repo_ref}"
