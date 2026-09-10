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
from .schema import ApmPolicy

if TYPE_CHECKING:
    from pathlib import Path

    from ..core.auth import AuthResolver
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
            from ..utils.git_env import git_remote_refs, git_subprocess_env

            probe_env = git_subprocess_env(git_env)
            probe_env["GIT_TERMINAL_PROMPT"] = "0"
            # A mocked or legacy resolver environment must not reintroduce
            # the deprecated raw-token transport into this subprocess.
            probe_env.pop("GIT_TOKEN", None)
            # auth-delegated: AuthResolver supplies the selected Git credential header.
            result = git_remote_refs(
                project_url,
                "HEAD",
                timeout=10,
                env=probe_env,
                options=("--exit-code",),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 0:
            return True
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


def first_concealed_closer_policy(
    skipped_namespaces: list[str],
    repo: str,
    *,
    host: str,
    port: int | None,
    project_root: Path | None = None,
    no_cache: bool = False,
    cache_only: bool = False,
) -> str | None:
    """Return the closest skipped namespace that must fail the walk closed.

    During the subgroup walk (see :func:`discovery._gitlab_walk_candidate`) an
    ``absent`` level is ambiguous -- GitLab returns 404 both for a missing
    project and for a private one the token cannot read. Before an ancestor
    policy is applied over the skipped closer levels, this confirms via
    authenticated Git whether any skipped closer ``apm-policy`` project actually
    exists. It returns the closest namespace the caller must fail closed on (a
    confirmed-present concealed project, or -- in ``cache_only`` mode -- one that
    cannot be verified offline), or ``None`` when every skipped level is known to
    be genuinely absent. ``skipped_namespaces`` is ordered closest-first.

    Only the DEFINITIVE ``present`` verdict is cached (per ``(host, namespace,
    repo)`` at the policy-cache TTL): ``_gitlab_project_state_via_git`` returns
    ``None`` for a missing project AND for an auth/network/timeout failure alike,
    so caching an ``absent`` verdict would let a transient failure suppress
    re-probing for the whole TTL and wrongly apply a weaker ancestor. Because
    absence is never definitive, the common "team inherits the org policy" path
    re-probes each install; that cost is accepted (see the PR trade-offs).

    ``cache_only`` (the offline local-bundle contract) never issues a network
    probe: a cached ``present`` fails closed, and an unverifiable level fails
    closed deterministically rather than reaching out to the network.
    """
    use_cache = project_root is not None and not no_cache
    for namespace in skipped_namespaces:
        if use_cache and _read_concealment_verdict(project_root, host, port, namespace, repo):
            return namespace  # cached definitive "present" -> concealed, fail closed
        if cache_only:
            # Offline: no network probe. Without a cached "present" we cannot
            # confirm this closer level is genuinely absent, so fail closed
            # rather than silently apply a weaker ancestor.
            return namespace
        if _gitlab_project_state_via_git(org=namespace, repo=repo, host=host, port=port) is True:
            if use_cache:
                _write_concealment_verdict(project_root, host, port, namespace, repo)
            return namespace
        # Indeterminate (None): missing OR transient failure -- NOT cached.
    return None


def _concealment_cache_file(project_root: Path, host: str, port: int | None, ns: str, repo: str):
    """Return the cache file path for one namespace's concealment verdict."""
    from .discovery import _cache_key, _get_cache_dir

    host_label = f"{host}:{port}" if port is not None else host
    key = _cache_key(f"concealment:{host_label}/{ns}/{repo}")
    return _get_cache_dir(project_root) / f"concealment_{key}.json"


def _read_concealment_verdict(
    project_root: Path, host: str, port: int | None, ns: str, repo: str
) -> bool:
    """Return ``True`` iff a fresh definitive ``"present"`` verdict is cached.

    Only ``present`` is ever cached (see :func:`first_concealed_closer_policy`),
    so a hit means the closer project is confirmed to exist.
    """
    import json
    import time

    from .discovery import CACHE_SCHEMA_VERSION, DEFAULT_CACHE_TTL

    try:
        raw = _concealment_cache_file(project_root, host, port, ns, repo).read_text(
            encoding="utf-8"
        )
        data = json.loads(raw)
    except (OSError, ValueError):
        return False
    if data.get("schema") != CACHE_SCHEMA_VERSION:
        return False
    if time.time() - float(data.get("ts", 0)) > DEFAULT_CACHE_TTL:
        return False
    return data.get("verdict") == "present"


def _write_concealment_verdict(
    project_root: Path, host: str, port: int | None, ns: str, repo: str
) -> None:
    """Persist the definitive ``present`` verdict (best-effort; never blocks)."""
    import json
    import time

    from .discovery import CACHE_SCHEMA_VERSION

    path = _concealment_cache_file(project_root, host, port, ns, repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema": CACHE_SCHEMA_VERSION, "verdict": "present", "ts": time.time()}),
            encoding="utf-8",
        )
    except OSError:
        return


def _fetch_gitlab_chain_parent(
    parent_ref: str,
    *,
    current_source: str,
    leaf_host: str,
    port: int | None,
    project_root: Path,
    no_cache: bool,
    cache_only: bool,
) -> PolicyFetchResult:
    """Fetch one GitLab policy parent through this adapter."""
    from .discovery import PolicyFetchResult

    current_parts = current_source.removeprefix("org:").split("/")
    current_org = current_parts[1] if len(current_parts) >= 3 else ""
    if parent_ref == "org":
        try:
            repo = _gitlab_policy_repo_candidates()[0]
        except ValueError:
            return PolicyFetchResult(
                source=f"org:{parent_ref}",
                error=f"Invalid GitLab policy reference: {parent_ref}",
                outcome="cache_miss_fetch_fail",
            )
        org = current_org
    else:
        parts = [p for p in parent_ref.strip("/").split("/") if p]
        invalid = PolicyFetchResult(
            source=f"org:{parent_ref}",
            error=f"Invalid GitLab policy reference: {parent_ref}",
            outcome="cache_miss_fetch_fail",
        )
        # A host-like first segment (FQDN or ``host:port``) MUST match the leaf
        # host and port exactly, then it is stripped so ``host/namespace/.../repo``
        # and ``namespace/.../repo`` are treated the same. A host-like segment
        # that does not match the leaf -- or carries a malformed port -- is a
        # cross-host/invalid ref and is rejected, never silently folded into the
        # namespace. A bare single-label first segment is a namespace segment
        # (GitLab nested subgroups, see #2753) and is left in place.
        if len(parts) >= 3 and ("." in parts[0] or ":" in parts[0]):
            try:
                explicit = urlsplit(f"//{parts[0]}")
                same_leaf = (
                    explicit.hostname is not None
                    and explicit.hostname.lower() == leaf_host.lower()
                    and explicit.port == port
                )
            except ValueError:
                same_leaf = False
            if not same_leaf:
                return invalid
            parts = parts[1:]
        # A GitLab namespace may be nested (subgroups): everything before the
        # final segment is the namespace, the final segment is the policy repo.
        # Requires at least ``namespace/repo``.
        if len(parts) < 2:
            return invalid
        org = "/".join(parts[:-1])
        repo = parts[-1]
    return _fetch_from_gitlab_repo(
        org=org,
        repo=repo,
        host=leaf_host,
        port=port,
        project_root=project_root,
        no_cache=no_cache,
        cache_only=cache_only,
    )


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
    """Fetch apm-policy.yml from a GitLab project through this adapter."""
    from .discovery import (
        PolicyFetchResult,
        _cache_entry_files_exist,
        _cache_only_policy_result,
        _read_cache_entry,
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
            return _gitlab_cached_policy_result(cache_entry, expected_hash)

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
        return _gitlab_fetch_error_result(
            error,
            cache_entry=cache_entry,
            source_label=source_label,
            org=org,
            repo=repo,
            host=host,
            port=port,
        )

    if content is None:
        return PolicyFetchResult(source=source_label, outcome="absent")

    return _gitlab_content_result(
        content,
        repo_ref=repo_ref,
        source_label=source_label,
        project_root=project_root,
        expected_hash=expected_hash,
        cache_entry=cache_entry,
    )


def _gitlab_cached_policy_result(
    cache_entry: _CacheEntry,
    expected_hash: str | None,
) -> PolicyFetchResult:
    """Build the cache-hit result without sharing another provider's flow."""
    from .discovery import PolicyFetchResult, _is_policy_empty

    result = PolicyFetchResult(
        policy=cache_entry.policy,
        source=cache_entry.source,
        expected_hash=expected_hash,
        warnings=cache_entry.warnings,
    )
    result.cached = True
    result.cache_age_seconds = cache_entry.age_seconds
    result.raw_bytes_hash = cache_entry.raw_bytes_hash or None
    result.outcome = "empty" if _is_policy_empty(cache_entry.policy) else "found"
    return result


def _gitlab_fetch_error_result(
    error: str,
    *,
    cache_entry: _CacheEntry | None,
    source_label: str,
    org: str,
    repo: str,
    host: str,
    port: int | None,
) -> PolicyFetchResult:
    """Preserve GitLab's typed absence and fail-closed ambiguous outcomes."""
    from .discovery import PolicyFetchResult, _stale_fallback_or_error

    if error.startswith("gitlab-status:404:"):
        return PolicyFetchResult(source=source_label, outcome="absent")
    if error.startswith("gitlab-status:410:"):
        _gitlab_project_state_via_git(org=org, repo=repo, host=host, port=port)
    return _stale_fallback_or_error(cache_entry, error, source_label, "cache_miss_fetch_fail")


def _gitlab_content_result(
    content: str,
    *,
    repo_ref: str,
    source_label: str,
    project_root: Path,
    expected_hash: str | None,
    cache_entry: _CacheEntry | None,
) -> PolicyFetchResult:
    """Parse, validate, and cache fetched GitLab policy content."""
    from .discovery import (
        PolicyFetchResult,
        _compute_hash_normalized,
        _detect_garbage,
        _verify_hash_pin,
    )

    for validation in (
        lambda: _detect_garbage(content, repo_ref, source_label, cache_entry),
        lambda: _verify_hash_pin(content, expected_hash, source_label),
    ):
        validation_result = validation()
        if validation_result is not None:
            return validation_result

    try:
        policy, warnings = load_policy(content)
    except PolicyValidationError as exc:
        return PolicyFetchResult(
            error=f"Invalid policy in {repo_ref}: {exc}",
            source=source_label,
            outcome="malformed",
            warnings=exc.warnings,
        )

    actual_hash = _compute_hash_normalized(content, expected_hash)
    if policy.extends is None:
        _cache_gitlab_leaf_policy(repo_ref, policy, project_root, actual_hash, warnings)
    return _gitlab_fresh_policy_result(
        policy,
        source_label=source_label,
        expected_hash=expected_hash,
        actual_hash=actual_hash,
        warnings=warnings,
    )


def _cache_gitlab_leaf_policy(
    repo_ref: str,
    policy: ApmPolicy,
    project_root: Path,
    actual_hash: str,
    warnings: list[str],
) -> None:
    """Persist an unextended GitLab leaf through the canonical cache writer."""
    from .discovery import _write_cache

    _write_cache(
        repo_ref,
        policy,
        project_root,
        **{
            "chain_refs": [repo_ref],
            "raw_bytes_hash": actual_hash,
            "warnings": warnings,
        },
    )


def _gitlab_fresh_policy_result(
    policy: ApmPolicy,
    *,
    source_label: str,
    expected_hash: str | None,
    actual_hash: str,
    warnings: list[str],
) -> PolicyFetchResult:
    """Build a fresh GitLab policy result with canonical value fields."""
    from .discovery import PolicyFetchResult, _is_policy_empty

    result = PolicyFetchResult(
        source=source_label,
        expected_hash=expected_hash,
        warnings=warnings,
    )
    result.policy = policy
    result.raw_bytes_hash = actual_hash
    result.outcome = "empty" if _is_policy_empty(policy) else "found"
    return result


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

    try:
        resp = _authenticated_gitlab_request(
            auth_resolver,
            api_url=api_url,
            host=host,
            org=org,
            port=port,
            project_path=project_path,
        )
        if resp.status_code in (404, 410):
            return None, f"gitlab-status:{resp.status_code}: Policy file not found"
        if 300 <= resp.status_code < 400:
            return None, f"Refusing HTTP redirect ({resp.status_code}) from {api_url}"
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code} fetching policy from {repo_ref}"
        return resp.text, None
    except requests.exceptions.Timeout:
        return None, f"Timeout fetching policy from {repo_ref}"
    except requests.exceptions.ConnectionError:
        return None, f"Connection error fetching policy from {repo_ref}"
    except RuntimeError as exc:
        return None, _gitlab_access_denied_error(
            auth_resolver,
            exc,
            host=host,
            org=org,
            port=port,
            repo_ref=repo_ref,
        )
    except requests.exceptions.RequestException:
        return None, f"Request error fetching policy from {repo_ref}"


def _authenticated_gitlab_request(
    auth_resolver: AuthResolver,
    *,
    api_url: str,
    host: str,
    org: str,
    port: int | None,
    project_path: str,
) -> requests.Response:
    """Run the GitLab REST request through the selected AuthResolver credential."""
    from ..core.auth import AuthResolver

    def _request(token: str | None, _git_env: dict[str, str]):
        response = requests.get(
            api_url,
            headers=AuthResolver.gitlab_rest_headers(token),
            timeout=10,
            allow_redirects=False,
        )
        if response.status_code in (401, 403):
            raise RuntimeError(f"{response.status_code}: unauthorized")
        return response

    return auth_resolver.try_with_fallback(
        host,
        _request,
        **{
            "org": org,
            "port": port,
            "path": project_path,
            "host_type": "gitlab",
            "unauth_first": False,
        },
    )


def _gitlab_access_denied_error(
    auth_resolver: AuthResolver,
    error: RuntimeError,
    *,
    host: str,
    org: str,
    port: int | None,
    repo_ref: str,
) -> str:
    """Render remediation without exposing the selected GitLab credential."""
    context = {"org": org}
    if port is not None:
        context["port"] = port
    remediation = auth_resolver.build_error_context(host, "fetch org policy", **context)
    return f"{error}: Access denied to {repo_ref}{remediation}"
