"""GitLab-specific org policy discovery (split from discovery.py, see #2566).

GitLab rejects project paths starting with ``.`` or ``_``, so none of the
default candidate repo names are valid there; GitLab uses its own
``apm-policy`` project convention, fetched via the Repository Files API.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

import requests

from .parser import PolicyValidationError, load_policy

if TYPE_CHECKING:
    from pathlib import Path

    from .discovery import PolicyFetchResult, _CacheEntry

# GitLab rejects project paths starting with ``.`` or ``_`` (see #2566), so
# none of the default candidates are valid there. ``apm-policy`` is the
# default GitLab policy-repo name; override via APM_GITLAB_POLICY_REPO for
# orgs that already use a different name.
_GITLAB_DEFAULT_POLICY_REPO = "apm-policy"
_GITLAB_PROJECT_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _validate_gitlab_project_segment(value: str, setting: str) -> str:
    """Return one valid GitLab project-name segment or raise ValueError."""
    candidate = value.strip()
    if candidate.startswith((".", "_")) or not _GITLAB_PROJECT_SEGMENT_RE.fullmatch(candidate):
        raise ValueError(
            f"{setting} must be one GitLab project-name segment "
            "(letters, digits, '.', '_' or '-', no slash, and no leading '.' or '_')"
        )
    return candidate


def _gitlab_policy_repo_candidates() -> tuple[str, ...]:
    """Return the (single) GitLab policy repo candidate, env-overridable."""
    candidate = os.environ.get("APM_GITLAB_POLICY_REPO", "") or _GITLAB_DEFAULT_POLICY_REPO
    return (_validate_gitlab_project_segment(candidate, "APM_GITLAB_POLICY_REPO"),)


def _gitlab_project_state_via_git(
    *,
    org: str,
    repo: str,
    host: str,
    port: int | None,
) -> bool | None:
    """Return whether authenticated Git confirms a GitLab project exists.

    ``None`` deliberately means that Git could not establish the project's
    state. It must not be treated as a missing policy because a disabled REST
    API, an unavailable network, and an auth failure are all governance
    failures rather than permission to skip policy enforcement.
    """
    from ..core.auth import AuthResolver
    from ..core.host_providers import HOST_PROVIDERS

    provider = HOST_PROVIDERS["gitlab"]
    api_base = urlsplit(provider.build_api_base(host, port))
    project_url = f"{api_base.scheme}://{api_base.netloc}/{org}/{repo}.git"
    resolver = AuthResolver()
    project_path = f"{org}/{repo}"

    def _probe(_token: str | None, git_env: dict[str, str]) -> bool | None:
        try:
            # auth-delegated: AuthResolver supplies the resolved Git credential environment.
            result = subprocess.run(
                ["git", "ls-remote", "--exit-code", project_url, "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, **git_env, "GIT_TERMINAL_PROMPT": "0"},
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 0:
            return True
        stderr = result.stderr.lower()
        if "repository not found" in stderr or "project not found" in stderr:
            return False
        return None

    try:
        return resolver.try_with_fallback(
            host,
            _probe,
            org=org,
            port=port,
            path=project_path,
            host_type="gitlab",
            unauth_first=False,
        )
    except RuntimeError:
        return None


def _fetch_from_gitlab_repo(
    *,
    org: str,
    repo: str,
    host: str,
    port: int | None = None,
    project_root: Path,
    no_cache: bool = False,
    expected_hash: str | None = None,
    cache_only: bool = False,
) -> PolicyFetchResult:
    """Fetch apm-policy.yml from a GitLab project.

    Mirrors ``_fetch_from_ado_repo`` but uses ``_fetch_gitlab_contents``
    (GitLab Repository Files API) instead of ``_fetch_ado_contents``.
    """
    from .discovery import (
        PolicyFetchResult,
        _cache_entry_files_exist,
        _cache_only_policy_result,
        _compute_hash_normalized,
        _detect_garbage,
        _is_policy_empty,
        _read_cache_entry,
        _stale_fallback_or_error,
        _verify_hash_pin,
        _write_cache,
    )

    try:
        repo = _validate_gitlab_project_segment(repo, "GitLab policy project")
    except ValueError as exc:
        return PolicyFetchResult(
            source=f"org:{host}/{org}/{repo}",
            error=str(exc),
            outcome="cache_miss_fetch_fail",
        )
    host_label = f"{host}:{port}" if port is not None else host
    project_path = f"{org}/{repo}"
    repo_ref = f"{host_label}/{project_path}"
    source_label = f"org:{repo_ref}"
    cache_entry: _CacheEntry | None = None
    cache_entry_persisted = _cache_entry_files_exist(repo_ref, project_root)

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
        return _cache_only_policy_result(
            cache_entry,
            source_label=source_label,
            expected_hash=expected_hash,
            cache_entry_persisted=cache_entry_persisted,
        )

    fetch_kwargs = {"host": host}
    if port is not None:
        fetch_kwargs["port"] = port
    content, error = _fetch_gitlab_contents(
        org,
        repo,
        "apm-policy.yml",
        **fetch_kwargs,
    )

    if error:
        # A typed 404 is GitLab's clean policy-absence signal. A 410 is
        # ambiguous: self-managed GitLab can return it for both a missing
        # project and a disabled Files API. Only authenticated Git can
        # disambiguate that case without silently bypassing governance.
        if error.startswith("gitlab-status:404:"):
            return PolicyFetchResult(source=source_label, outcome="absent")
        if error.startswith("gitlab-status:410:"):
            if _gitlab_project_state_via_git(org=org, repo=repo, host=host, port=port) is False:
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
    if policy.extends is None:
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


def _fetch_gitlab_contents(
    org: str,
    repo: str,
    file_path: str,
    *,
    host: str = "gitlab.com",
    port: int | None = None,
) -> tuple[str | None, str | None]:
    """Fetch file contents from a GitLab project via the Repository Files API.

    Returns ``(content_string, error_string)``. One will be ``None``.
    """
    from ..core.auth import AuthResolver
    from ..core.host_providers import HOST_PROVIDERS

    project_path = f"{org}/{repo}"
    host_label = f"{host}:{port}" if port is not None else host
    repo_ref = f"{host_label}/{project_path}"

    api_base = HOST_PROVIDERS["gitlab"].build_api_base(host, port)
    encoded_project = quote(project_path, safe="")
    encoded_file = quote(file_path, safe="")
    # "HEAD" resolves to the project's default branch without requiring
    # callers to know its name up front.
    api_url = f"{api_base}/projects/{encoded_project}/repository/files/{encoded_file}/raw?ref=HEAD"

    auth_resolver = AuthResolver()

    def _request(token: str | None, _git_env: dict[str, str]):
        headers = AuthResolver.gitlab_rest_headers(token)
        response = requests.get(
            api_url,
            headers=headers,
            timeout=10,
            allow_redirects=False,
        )
        if response.status_code in (401, 403):
            raise RuntimeError(f"{response.status_code}: unauthorized")
        return response

    try:
        resp = auth_resolver.try_with_fallback(
            host,
            _request,
            org=org,
            port=port,
            path=project_path,
            host_type="gitlab",
            unauth_first=False,
        )
        if resp.status_code in (404, 410):
            return None, f"gitlab-status:{resp.status_code}: Policy file not found"
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("Location", "<no Location header>")
            return None, (
                f"Refusing HTTP redirect ({resp.status_code}) from {api_url} to {location}"
            )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code} fetching policy from {repo_ref}"
        return resp.text, None
    except requests.exceptions.Timeout:
        return None, f"Timeout fetching policy from {repo_ref}"
    except requests.exceptions.ConnectionError:
        return None, f"Connection error fetching policy from {repo_ref}"
    except RuntimeError as exc:
        error_kwargs = {"org": org}
        if port is not None:
            error_kwargs["port"] = port
        remediation = auth_resolver.build_error_context(
            host,
            "fetch org policy",
            **error_kwargs,
        )
        return None, f"{exc}: Access denied to {repo_ref}{remediation}"
    except Exception as e:
        return None, f"Error fetching policy from {repo_ref}: {e}"
