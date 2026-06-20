"""Azure DevOps policy-fetch helpers extracted from ``discovery.py`` (#1078).

The strangler-fig refactor keeps ``discovery.py`` under the source
file-length budget by relocating the ADO transport here.  To preserve the
existing test seams, every discovery-level symbol (``requests``, the cache
helpers, ``_fetch_ado_contents``) is resolved through the ``discovery``
module object at call time -- so patches targeting
``apm_cli.policy.discovery.<name>`` continue to intercept these functions
after relocation.  ``discovery`` re-exports both functions, so callers and
test patch targets keep using ``apm_cli.policy.discovery._fetch_from_ado_repo``
and ``..._fetch_ado_contents`` unchanged.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from ..utils.github_host import build_ado_api_url

if TYPE_CHECKING:
    from pathlib import Path

    from ._discovery_cache import _CacheEntry
    from .discovery import PolicyFetchResult


def _fetch_from_ado_repo(
    *,
    org: str,
    project: str,
    repo: str,
    host: str,
    project_root: Path,
    no_cache: bool = False,
    expected_hash: str | None = None,
) -> PolicyFetchResult:
    """Fetch apm-policy.yml from an Azure DevOps repo.

    Mirrors ``_fetch_from_repo`` but uses ``_fetch_ado_contents`` (ADO
    Items API) instead of ``_fetch_github_contents`` (GitHub Contents API).
    """
    from apm_cli.policy import discovery as _d

    repo_ref = f"{host}/{org}/{project}/{repo}"
    source_label = f"org:{repo_ref}"
    cache_entry: _CacheEntry | None = None

    if not no_cache:
        cache_entry = _d._read_cache_entry(repo_ref, project_root, expected_hash=expected_hash)
        if cache_entry is not None and not cache_entry.stale:
            outcome = "empty" if _d._is_policy_empty(cache_entry.policy) else "found"
            return _d.PolicyFetchResult(
                policy=cache_entry.policy,
                source=cache_entry.source,
                cached=True,
                cache_age_seconds=cache_entry.age_seconds,
                outcome=outcome,
                raw_bytes_hash=cache_entry.raw_bytes_hash or None,
                expected_hash=expected_hash,
            )

    content, error = _d._fetch_ado_contents(org, project, repo, "apm-policy.yml", host=host)

    if error:
        if "404" in error:
            return _d.PolicyFetchResult(source=source_label, outcome="absent")
        return _d._stale_fallback_or_error(
            cache_entry, error, source_label, "cache_miss_fetch_fail"
        )

    if content is None:
        return _d.PolicyFetchResult(source=source_label, outcome="absent")

    garbage_result = _d._detect_garbage(content, repo_ref, source_label, cache_entry)
    if garbage_result is not None:
        return garbage_result

    mismatch = _d._verify_hash_pin(content, expected_hash, source_label)
    if mismatch is not None:
        return mismatch

    try:
        policy, _warnings = _d.load_policy(content)
    except _d.PolicyValidationError as e:
        return _d.PolicyFetchResult(
            error=f"Invalid policy in {repo_ref}: {e}",
            source=source_label,
            outcome="malformed",
        )

    chain_refs = [repo_ref]
    actual_hash = _d._compute_hash_normalized(content, expected_hash)
    _d._write_cache(
        repo_ref,
        policy,
        project_root,
        chain_refs=chain_refs,
        raw_bytes_hash=actual_hash,
    )
    outcome = "empty" if _d._is_policy_empty(policy) else "found"
    return _d.PolicyFetchResult(
        policy=policy,
        source=source_label,
        outcome=outcome,
        raw_bytes_hash=actual_hash,
        expected_hash=expected_hash,
    )


def _fetch_ado_contents(
    org: str,
    project: str,
    repo: str,
    file_path: str,
    *,
    host: str = "dev.azure.com",
) -> tuple[str | None, str | None]:
    """Fetch file contents from Azure DevOps Items API.

    Returns ``(content_string, error_string)``. One will be ``None``.
    """
    from apm_cli.policy import discovery as _d

    api_url = build_ado_api_url(org, project, repo, file_path, host=host)
    repo_ref = f"{host}/{org}/{project}/{repo}"

    # ADO auth is centralized in AuthResolver: ADO_APM_PAT uses Basic auth,
    # and az CLI AAD tokens use Bearer auth. No GitHub PATs are consulted.
    from ..core.auth import AuthResolver

    headers: dict[str, str] = {}
    auth_resolver = AuthResolver()
    auth_ctx = auth_resolver.resolve(host, org=org)
    if auth_ctx.token:
        if auth_ctx.auth_scheme == "bearer":
            headers["Authorization"] = f"Bearer {auth_ctx.token}"
        else:
            basic_cred = base64.b64encode(f":{auth_ctx.token}".encode()).decode()
            headers["Authorization"] = f"Basic {basic_cred}"

    try:
        resp = _d.requests.get(api_url, headers=headers, timeout=10, allow_redirects=False)
        if resp.status_code == 404:
            return None, "404: Policy file not found"
        if resp.status_code in (401, 403):
            remediation = auth_resolver.build_error_context(host, "fetch org policy", org=org)
            return None, (f"{resp.status_code}: Access denied to {repo_ref}{remediation}")
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("Location", "<no Location header>")
            return None, (
                f"Refusing HTTP redirect ({resp.status_code}) from {api_url} to {location}"
            )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code} fetching policy from {repo_ref}"
        # ADO Items API returns raw file content by default
        return resp.text, None
    except _d.requests.exceptions.Timeout:
        return None, f"Timeout fetching policy from {repo_ref}"
    except _d.requests.exceptions.ConnectionError:
        return None, f"Connection error fetching policy from {repo_ref}"
    except Exception as e:
        return None, f"Error fetching policy from {repo_ref}: {e}"
