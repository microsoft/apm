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
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests
import yaml

from .parser import PolicyValidationError, load_policy
from .schema import ApmPolicy

logger = logging.getLogger(__name__)

# Cache location: apm_modules/.policy-cache/<hash>.yml + <hash>.meta.json
POLICY_CACHE_DIR = ".policy-cache"
DEFAULT_CACHE_TTL = 3600  # 1 hour
MAX_STALE_TTL = 7 * 24 * 3600  # 7 days -- stale cache usable on refresh failure
CACHE_SCHEMA_VERSION = "2"  # Bump when cache format changes to auto-invalidate


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
    """

    policy: Optional[ApmPolicy] = None
    source: str = ""  # "org:contoso/.github", "file:/path", "url:https://..."
    cached: bool = False  # True if served from cache
    error: Optional[str] = None  # Error message if fetch failed

    # -- Outcome-matrix fields (W1-cache-redesign) --
    cache_age_seconds: Optional[int] = None  # Age of cache entry in seconds
    cache_stale: bool = False  # True if cache was served past TTL
    fetch_error: Optional[str] = None  # Network/parse error on refresh attempt
    outcome: str = ""  # See docstring for valid values

    @property
    def found(self) -> bool:
        return self.policy is not None


def discover_policy(
    project_root: Path,
    *,
    policy_override: Optional[str] = None,
    no_cache: bool = False,
) -> PolicyFetchResult:
    """Discover and load the applicable policy for a project.

    Resolution order:
    1. If policy_override is a local file path -> load from file
    2. If policy_override is a URL -> fetch from URL
    3. If policy_override is "org" -> auto-discover from org
    4. If policy_override is None -> auto-discover from org
    """
    if policy_override:
        path = Path(policy_override)
        if path.exists() and path.is_file():
            return _load_from_file(path)
        if policy_override.startswith("http://"):
            return PolicyFetchResult(
                error="Refusing plaintext http:// policy URL -- use https://",
                source=f"url:{policy_override}",
            )
        if policy_override.startswith("https://"):
            return _fetch_from_url(policy_override, project_root, no_cache=no_cache)
        if policy_override != "org":
            # Try as owner/repo reference
            return _fetch_from_repo(
                policy_override, project_root, no_cache=no_cache
            )

    # Auto-discover from git remote
    return _auto_discover(project_root, no_cache=no_cache)


def _load_from_file(path: Path) -> PolicyFetchResult:
    """Load policy from a local file."""
    try:
        policy, _warnings = load_policy(path)
        outcome = "empty" if _is_policy_empty(policy) else "found"
        return PolicyFetchResult(
            policy=policy, source=f"file:{path}", outcome=outcome
        )
    except PolicyValidationError as e:
        return PolicyFetchResult(
            error=f"Invalid policy file {path}: {e}", outcome="malformed"
        )
    except Exception as e:
        return PolicyFetchResult(
            error=f"Failed to read {path}: {e}",
            outcome="cache_miss_fetch_fail",
        )


def _auto_discover(
    project_root: Path, *, no_cache: bool = False
) -> PolicyFetchResult:
    """Auto-discover policy from org's .github repo.

    1. Run git remote get-url origin
    2. Parse org from URL
    3. Fetch <org>/.github/apm-policy.yml
    """
    org_and_host = _extract_org_from_git_remote(project_root)
    if org_and_host is None:
        return PolicyFetchResult(
            error="Could not determine org from git remote",
            outcome="no_git_remote",
        )

    org, host = org_and_host
    repo_ref = f"{org}/.github"
    if host and host != "github.com":
        repo_ref = f"{host}/{repo_ref}"

    return _fetch_from_repo(repo_ref, project_root, no_cache=no_cache)


def _extract_org_from_git_remote(
    project_root: Path,
) -> Optional[Tuple[str, str]]:
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


def _parse_remote_url(url: str) -> Optional[Tuple[str, str]]:
    """Parse a git remote URL into (org, host).

    Returns None if URL can't be parsed.
    """
    if not url:
        return None

    # SSH: git@github.com:owner/repo.git
    if url.startswith("git@"):
        try:
            host_part, path_part = url.split(":", 1)
            host = host_part.replace("git@", "")
            parts = path_part.rstrip("/").removesuffix(".git").split("/")
            if parts and parts[0]:
                return (parts[0], host)
        except (ValueError, IndexError):
            return None
        return None

    # HTTPS: https://github.com/owner/repo.git
    # ADO:   https://dev.azure.com/org/project/_git/repo
    if "://" in url:
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            path_parts = (
                parsed.path.strip("/").removesuffix(".git").rstrip("/").split("/")
            )
            if host and path_parts and path_parts[0]:
                return (path_parts[0], host)
        except Exception:
            return None

    return None


def _fetch_from_url(
    url: str,
    project_root: Path,
    *,
    no_cache: bool = False,
) -> PolicyFetchResult:
    """Fetch policy YAML from a direct URL."""
    source_label = f"url:{url}"
    cache_entry: Optional[_CacheEntry] = None

    # Use URL as cache key
    if not no_cache:
        cache_entry = _read_cache_entry(url, project_root)
        if cache_entry is not None and not cache_entry.stale:
            outcome = "empty" if _is_policy_empty(cache_entry.policy) else "found"
            return PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_age_seconds=cache_entry.age_seconds,
                outcome=outcome,
            )

    fetch_error: Optional[str] = None
    content: Optional[str] = None

    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 404:
            return PolicyFetchResult(
                source=source_label,
                error="404: Policy file not found",
                outcome="absent",
            )
        if resp.status_code != 200:
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

    # Garbage-response detection: body must be valid YAML mapping
    garbage_result = _detect_garbage(content, url, source_label, cache_entry)
    if garbage_result is not None:
        return garbage_result

    try:
        policy, _warnings = load_policy(content)
    except PolicyValidationError as e:
        return PolicyFetchResult(
            error=f"Invalid policy from {url}: {e}",
            source=source_label,
            outcome="malformed",
        )

    chain_refs = [url]
    _write_cache(url, policy, project_root, chain_refs=chain_refs)
    outcome = "empty" if _is_policy_empty(policy) else "found"
    return PolicyFetchResult(policy=policy, source=source_label, outcome=outcome)


def _fetch_from_repo(
    repo_ref: str,
    project_root: Path,
    *,
    no_cache: bool = False,
) -> PolicyFetchResult:
    """Fetch apm-policy.yml from a GitHub repo via Contents API.

    repo_ref format: "owner/.github" or "host/owner/.github"
    """
    source_label = f"org:{repo_ref}"
    cache_entry: Optional[_CacheEntry] = None

    if not no_cache:
        cache_entry = _read_cache_entry(repo_ref, project_root)
        if cache_entry is not None and not cache_entry.stale:
            outcome = "empty" if _is_policy_empty(cache_entry.policy) else "found"
            return PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_age_seconds=cache_entry.age_seconds,
                outcome=outcome,
            )

    content, error = _fetch_github_contents(repo_ref, "apm-policy.yml")

    if error:
        # 404 = no policy, not an error
        if "404" in error:
            return PolicyFetchResult(source=source_label, outcome="absent")
        # Fetch failed -- try stale cache fallback
        return _stale_fallback_or_error(
            cache_entry, error, source_label, "cache_miss_fetch_fail"
        )

    if content is None:
        return PolicyFetchResult(source=source_label, outcome="absent")

    # Garbage-response detection
    garbage_result = _detect_garbage(content, repo_ref, source_label, cache_entry)
    if garbage_result is not None:
        return garbage_result

    try:
        policy, _warnings = load_policy(content)
    except PolicyValidationError as e:
        return PolicyFetchResult(
            error=f"Invalid policy in {repo_ref}: {e}",
            source=source_label,
            outcome="malformed",
        )

    chain_refs = [repo_ref]
    _write_cache(repo_ref, policy, project_root, chain_refs=chain_refs)
    outcome = "empty" if _is_policy_empty(policy) else "found"
    return PolicyFetchResult(policy=policy, source=source_label, outcome=outcome)


def _fetch_github_contents(
    repo_ref: str,
    file_path: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Fetch file contents from GitHub API.

    Returns (content_string, error_string). One will be None.
    """

    # Parse repo_ref: "owner/repo" or "host/owner/repo"
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

    # Build API URL
    if host == "github.com":
        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
    else:
        api_url = (
            f"https://{host}/api/v3/repos/{owner}/{repo}/contents/{file_path}"
        )

    headers = {"Accept": "application/vnd.github.v3+json"}
    token = _get_token_for_host(host)
    if token:
        headers["Authorization"] = f"token {token}"

    try:
        resp = requests.get(api_url, headers=headers, timeout=10)
        if resp.status_code == 404:
            return None, "404: Policy file not found"
        if resp.status_code == 403:
            return None, f"403: Access denied to {repo_ref}"
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code} fetching policy from {repo_ref}"

        data = resp.json()
        if data.get("encoding") == "base64" and data.get("content"):
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content, None
        elif data.get("content"):
            return data["content"], None
        else:
            return None, f"Unexpected response format from {repo_ref}"
    except requests.exceptions.Timeout:
        return None, f"Timeout fetching policy from {repo_ref}"
    except requests.exceptions.ConnectionError:
        return None, f"Connection error fetching policy from {repo_ref}"
    except Exception as e:
        return None, f"Error fetching policy from {repo_ref}: {e}"


def _is_github_host(host: str) -> bool:
    """Return True if *host* is a known GitHub-family hostname."""
    if host == "github.com":
        return True
    if host.endswith(".ghe.com"):
        return True
    gh_host = os.environ.get("GITHUB_HOST", "")
    if gh_host and host == gh_host:
        return True
    return False


def _get_token_for_host(host: str) -> Optional[str]:
    """Get authentication token for a given host.

    Environment-variable tokens (GITHUB_TOKEN, GITHUB_APM_PAT, GH_TOKEN)
    are only returned when *host* is a recognized GitHub-family hostname.
    For other hosts the token manager + git credential helpers are used.
    """
    try:
        from ..core.token_manager import GitHubTokenManager

        manager = GitHubTokenManager()
        return manager.get_token_with_credential_fallback("modules", host)
    except Exception:
        if _is_github_host(host):
            return (
                os.environ.get("GITHUB_TOKEN")
                or os.environ.get("GITHUB_APM_PAT")
                or os.environ.get("GH_TOKEN")
            )
        return None


# -- Cache ----------------------------------------------------------


@dataclass
class _CacheEntry:
    """Internal representation of a cached policy read."""

    policy: ApmPolicy
    source: str
    age_seconds: int
    stale: bool  # True if past TTL (but within MAX_STALE_TTL)
    chain_refs: List[str] = field(default_factory=list)
    fingerprint: str = ""


def _get_cache_dir(project_root: Path) -> Path:
    """Get the policy cache directory."""
    return project_root / "apm_modules" / POLICY_CACHE_DIR


def _cache_key(repo_ref: str) -> str:
    """Generate a deterministic cache filename from repo ref."""
    return hashlib.sha256(repo_ref.encode()).hexdigest()[:16]


def _policy_to_dict(policy: ApmPolicy) -> dict:
    """Serialize an ApmPolicy to a dict matching the YAML schema."""

    def _opt_list(val: Optional[Tuple[str, ...]]) -> Optional[list]:
        return None if val is None else list(val)

    return {
        "name": policy.name,
        "version": policy.version,
        "enforcement": policy.enforcement,
        "cache": {"ttl": policy.cache.ttl},
        "dependencies": {
            "allow": _opt_list(policy.dependencies.allow),
            "deny": list(policy.dependencies.deny),
            "require": list(policy.dependencies.require),
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
            "directories": list(policy.unmanaged_files.directories),
        },
    }


def _serialize_policy(policy: ApmPolicy) -> str:
    """Serialize an ApmPolicy to deterministic YAML for caching."""
    return yaml.dump(
        _policy_to_dict(policy), default_flow_style=False, sort_keys=True
    )


def _policy_fingerprint(serialized: str) -> str:
    """Compute a fingerprint of a serialized policy."""
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]


def _is_policy_empty(policy: ApmPolicy) -> bool:
    """Return True if a policy has no actionable restrictions.

    An 'empty' policy is syntactically valid but imposes no constraints
    beyond the permissive defaults.
    """
    return (
        not policy.dependencies.deny
        and policy.dependencies.allow is None
        and not policy.dependencies.require
        and not policy.mcp.deny
        and policy.mcp.allow is None
        and policy.mcp.transport.allow is None
        and policy.compilation.target.allow is None
        and not policy.manifest.required_fields
        and policy.manifest.scripts == "allow"
        and policy.manifest.content_types is None
        and policy.unmanaged_files.action == "ignore"
    )


def _stale_fallback_or_error(
    cache_entry: Optional[_CacheEntry],
    fetch_error_msg: str,
    source_label: str,
    outcome_on_miss: str,
) -> PolicyFetchResult:
    """Return stale cache if available, otherwise error with given outcome."""
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
    content: Optional[str],
    identifier: str,
    source_label: str,
    cache_entry: Optional[_CacheEntry],
) -> Optional[PolicyFetchResult]:
    """Detect garbage responses (200 OK with non-YAML body).

    Returns a PolicyFetchResult if the content is garbage (stale fallback
    or garbage_response outcome), or None if the content looks parseable.
    """
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
) -> Optional[_CacheEntry]:
    """Read cache entry with stale-awareness.

    Returns:
    * ``_CacheEntry(stale=False)`` -- within TTL, ready for immediate use
    * ``_CacheEntry(stale=True)``  -- past TTL but within MAX_STALE_TTL
    * ``None``                     -- no cache file, corrupt, or past MAX_STALE_TTL
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
        )
    except Exception:
        return None


def _read_cache(
    repo_ref: str,
    project_root: Path,
    ttl: int = DEFAULT_CACHE_TTL,
) -> Optional[PolicyFetchResult]:
    """Read policy from cache if still valid (within TTL).

    Legacy wrapper around ``_read_cache_entry`` for backward compatibility.
    Returns None if cache miss, expired, or past MAX_STALE_TTL.
    """
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
    chain_refs: Optional[List[str]] = None,
) -> None:
    """Write merged effective policy and metadata to cache atomically.

    Uses temp file + ``os.replace()`` to prevent torn writes from parallel
    installs.  Both the policy file and metadata sidecar are written
    atomically and independently.
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
        try:
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
    }
    tmp_meta = cache_dir / f"{key}.{uid}.meta.json.tmp"
    try:
        tmp_meta.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(str(tmp_meta), str(meta_file))
    except OSError:
        try:
            tmp_meta.unlink(missing_ok=True)
        except OSError:
            pass
