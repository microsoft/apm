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

import base64
import logging
import os
import subprocess
import threading  # noqa: F401
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from ..cache.url_normalize import SCP_LIKE_RE
from ..utils.github_host import (
    is_azure_devops_hostname,
    is_visualstudio_legacy_hostname,
)
from ._discovery_ado import (
    _fetch_ado_contents as _fetch_ado_contents,
)
from ._discovery_ado import (
    _fetch_from_ado_repo as _fetch_from_ado_repo,
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
    _cache_key as _cache_key,
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
    _verify_hash_pin as _verify_hash_pin,
)
from ._discovery_cache import (
    _write_cache as _write_cache,
)
from ._discovery_chain import (
    _derive_leaf_host as _derive_leaf_host,
)
from ._discovery_chain import (
    _extract_extends_host as _extract_extends_host,
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
from .parser import PolicyValidationError, load_policy
from .project_config import (
    ProjectPolicyConfigError,
    read_project_policy_hash_pin,
)
from .schema import ApmPolicy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy repo discovery: cascading candidate repos per host profile
# ---------------------------------------------------------------------------

# Candidate repo names in precedence order (first valid policy wins).
# Host profiles select which candidates are valid for a given git host.
_DEFAULT_POLICY_REPOS: tuple[str, ...] = (".github", ".apm", "_apm")
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

    @property
    def found(self) -> bool:
        return self.policy is not None


def discover_policy_with_chain(
    project_root: Path,
    *,
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

    # -- Base discovery ------------------------------------------------
    fetch_result = discover_policy(project_root, expected_hash=expected_hash)

    # -- Chain resolution if leaf has extends: -------------------------
    if (
        fetch_result.policy is not None
        and fetch_result.outcome in ("found", "cached_stale")
        and fetch_result.policy.extends is not None
        and not fetch_result.cached  # Don't re-resolve if served from cache
    ):
        _resolve_and_persist_chain(fetch_result, project_root)

    return fetch_result


def discover_policy(
    project_root: Path,
    *,
    policy_override: str | None = None,
    no_cache: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Discover and load the applicable policy for a project.

    Resolution order:
    1. If policy_override is a local file path -> load from file
    2. If policy_override is an https:// URL -> fetch from URL
       (http:// is rejected for security)
    3. If policy_override is "org" -> auto-discover from project's git remote
    4. If policy_override is "owner/repo" (or "host/owner/repo")
       -> fetch from that repo via GitHub Contents API
    5. If policy_override is None -> auto-discover from project's git remote

    The optional ``expected_hash`` (``"<algo>:<hex>"``) pins the leaf
    policy bytes; mismatches return ``outcome="hash_mismatch"`` and
    must always be treated fail-closed by callers.
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
                expected_hash=expected_hash,
            )

    # Auto-discover from git remote
    return _auto_discover(project_root, no_cache=no_cache, expected_hash=expected_hash)


def _load_from_file(path: Path, *, expected_hash: str | None = None) -> PolicyFetchResult:
    """Load policy from a local file."""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        return PolicyFetchResult(
            error=f"Failed to read {path}: {e}",
            outcome="cache_miss_fetch_fail",
        )

    source_label = f"file:{path}"
    mismatch = _verify_hash_pin(content, expected_hash, source_label)
    if mismatch is not None:
        return mismatch

    try:
        policy, _warnings = load_policy(content)
        outcome = "empty" if _is_policy_empty(policy) else "found"
        actual_hash = (
            _compute_hash_normalized(content, expected_hash) if expected_hash is not None else None
        )
        return PolicyFetchResult(
            policy=policy,
            source=source_label,
            outcome=outcome,
            raw_bytes_hash=actual_hash,
            expected_hash=expected_hash,
        )
    except PolicyValidationError as e:
        return PolicyFetchResult(error=f"Invalid policy file {path}: {e}", outcome="malformed")


def _auto_discover(
    project_root: Path,
    *,
    no_cache: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Auto-discover policy by cascading through candidate repos.

    1. Run git remote get-url origin
    2. Parse org + host from URL
    3. Select host profile to determine candidate repos
    4. Try each candidate in precedence order (.github > .apm > _apm)
       - 404/absent -> continue to next candidate
       - Error (auth, timeout, malformed) -> fail-closed immediately
       - Found -> return (first match wins)
    5. All candidates exhausted -> outcome="absent"
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


def _parse_remote_url(url: str) -> tuple[str, str] | None:
    """Parse a git remote URL into (org, host).

    Accepts SCP-style SSH URLs with any username (not just ``git@``), so
    EMU/GHE deployments that use a non-``git`` SSH user parse correctly.
    Also handles Azure DevOps SSH URLs (``v3/`` path prefix).

    Returns None if URL can't be parsed.
    """
    if not url:
        return None

    scp_match = SCP_LIKE_RE.match(url)
    if scp_match:
        host = scp_match.group("host")
        path_part = scp_match.group("path")
        try:
            parts = path_part.rstrip("/").removesuffix(".git").split("/")
            parts = [p for p in parts if p]
            if not parts:
                return None
            if host == "ssh.dev.azure.com" and parts[0] == "v3" and len(parts) >= 2:
                return (parts[1], host)
            return (parts[0], host)
        except (ValueError, IndexError):
            return None

    if "://" in url:
        return _parse_scheme_remote_url(url)

    return None


def _parse_scheme_remote_url(url: str) -> tuple[str, str] | None:
    """Parse a scheme-style remote URL (``https://host/org/...``).

    Azure DevOps legacy ``*.visualstudio.com`` hosts encode the org in the
    hostname rather than the path, so they are handled before the generic
    path-based extraction.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path_parts = parsed.path.strip("/").removesuffix(".git").rstrip("/").split("/")
        if is_visualstudio_legacy_hostname(host):
            return (host[: -len(".visualstudio.com")], host)
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
        policy, _warnings = load_policy(content)
    except PolicyValidationError as e:
        return PolicyFetchResult(
            error=f"Invalid policy from {url}: {e}",
            source=source_label,
            outcome="malformed",
        )

    chain_refs = [url]
    actual_hash = _compute_hash_normalized(content, expected_hash)
    _write_cache(url, policy, project_root, chain_refs=chain_refs, raw_bytes_hash=actual_hash)
    outcome = "empty" if _is_policy_empty(policy) else "found"
    return PolicyFetchResult(
        policy=policy,
        source=source_label,
        outcome=outcome,
        raw_bytes_hash=actual_hash,
        expected_hash=expected_hash,
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
            )

    content, error = _fetch_github_contents(repo_ref, "apm-policy.yml")

    if error:
        if "404" in error:
            return PolicyFetchResult(source=source_label, outcome="absent")
        return _stale_fallback_or_error(cache_entry, error, source_label, "cache_miss_fetch_fail")

    if content is None:
        return PolicyFetchResult(source=source_label, outcome="absent")

    garbage_result = _detect_garbage(content, repo_ref, source_label, cache_entry)
    if garbage_result is not None:
        return garbage_result

    mismatch = _verify_hash_pin(content, expected_hash, source_label)
    if mismatch is not None:
        return mismatch

    try:
        policy, _warnings = load_policy(content)
    except PolicyValidationError as e:
        return PolicyFetchResult(
            error=f"Invalid policy in {repo_ref}: {e}",
            source=source_label,
            outcome="malformed",
        )

    chain_refs = [repo_ref]
    actual_hash = _compute_hash_normalized(content, expected_hash)
    _write_cache(repo_ref, policy, project_root, chain_refs=chain_refs, raw_bytes_hash=actual_hash)
    outcome = "empty" if _is_policy_empty(policy) else "found"
    return PolicyFetchResult(
        policy=policy,
        source=source_label,
        outcome=outcome,
        raw_bytes_hash=actual_hash,
        expected_hash=expected_hash,
    )


# ---------------------------------------------------------------------------
# GitHub API helpers -- decomposed to keep _fetch_github_contents <= 8 returns
# ---------------------------------------------------------------------------


def _parse_github_repo_ref(repo_ref: str) -> tuple[str, str, str] | None:
    """Parse repo_ref into (host, owner, repo_path), or None if invalid."""
    parts = repo_ref.split("/")
    if len(parts) == 2:
        return ("github.com", parts[0], parts[1])
    if len(parts) >= 3:
        return (parts[0], parts[1], "/".join(parts[2:]))
    return None


def _decode_github_content(data: dict, repo_ref: str) -> tuple[str | None, str | None]:
    """Decode GitHub API response body to (content_str, error_str)."""
    if data.get("encoding") == "base64" and data.get("content"):
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, None
    if data.get("content"):
        return data["content"], None
    return None, f"Unexpected response format from {repo_ref}"


def _call_github_api(
    api_url: str,
    headers: dict,
    repo_ref: str,
) -> tuple[str | None, str | None]:
    """Call GitHub Contents API and return (content_str, error_str)."""
    try:
        resp = requests.get(api_url, headers=headers, timeout=10, allow_redirects=False)
    except requests.exceptions.Timeout:
        return None, f"Timeout fetching policy from {repo_ref}"
    except requests.exceptions.ConnectionError:
        return None, f"Connection error fetching policy from {repo_ref}"
    except Exception as e:
        return None, f"Error fetching policy from {repo_ref}: {e}"

    if resp.status_code == 404:
        return None, "404: Policy file not found"
    if resp.status_code == 403:
        return None, f"403: Access denied to {repo_ref}"
    if 300 <= resp.status_code < 400:
        location = resp.headers.get("Location", "<no Location header>")
        return None, (f"Refusing HTTP redirect ({resp.status_code}) from {api_url} to {location}")
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code} fetching policy from {repo_ref}"
    return _decode_github_content(resp.json(), repo_ref)


def _fetch_github_contents(
    repo_ref: str,
    file_path: str,
) -> tuple[str | None, str | None]:
    """Fetch file contents from GitHub API.

    Returns (content_string, error_string). One will be None.
    """
    parsed = _parse_github_repo_ref(repo_ref)
    if parsed is None:
        return None, f"Invalid repo reference: {repo_ref}"

    host, owner, repo = parsed
    if host == "github.com":
        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
    else:
        api_url = f"https://{host}/api/v3/repos/{owner}/{repo}/contents/{file_path}"

    headers = {"Accept": "application/vnd.github.v3+json"}
    token = _get_token_for_host(host)
    if token:
        headers["Authorization"] = f"token {token}"

    return _call_github_api(api_url, headers, repo_ref)


def _is_github_host(host: str) -> bool:
    """Return True if *host* is a known GitHub-family hostname."""
    if host == "github.com":
        return True
    if host.endswith(".ghe.com"):
        return True
    gh_host = os.environ.get("GITHUB_HOST", "")
    if gh_host and host == gh_host:  # noqa: SIM103
        return True
    return False


def _get_token_for_host(host: str) -> str | None:
    """Get authentication token for a given host.

    Environment-variable tokens (GITHUB_TOKEN, GITHUB_APM_PAT, GH_TOKEN)
    are only returned when *host* is a recognized GitHub-family hostname.
    For other hosts the token manager + git credential helpers are used.
    """
    try:
        from ..core.token_manager import GitHubTokenManager

        manager = GitHubTokenManager()
        return manager.get_token_with_credential_fallback("modules", host)
    except Exception as exc:
        logger.debug("Token manager failed for %s: %s", host, exc)
        if _is_github_host(host):
            return (
                os.environ.get("GITHUB_TOKEN")
                or os.environ.get("GITHUB_APM_PAT")
                or os.environ.get("GH_TOKEN")
            )
        return None
