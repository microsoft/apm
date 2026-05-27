"""Authoritative Artifactory boundary resolution for install-time validation.

Native GitLab support (see :mod:`apm_cli.install.gitlab_resolver`) probes the
GitLab API to find the real repository boundary inside a nested subgroup path.
For deps routed through a JFrog Artifactory VCS proxy the proxy itself serves
archive ZIPs at deterministic URLs, so the archive *is* the existence signal:
``HEAD`` the candidate archive URL, treat ``2xx-3xx`` as existence proof.

The resolver covers both routing modes uniformly:

* **Mode 1** -- explicit FQDN deps (``host/artifactory/key/owner/repo/...``).
  Host and prefix come from the dependency reference itself.
* **Mode 2** -- bare shorthand (``owner/repo/...``) routed through the proxy
  by ``PROXY_REGISTRY_URL`` + ``PROXY_REGISTRY_ONLY=1``.  Host and prefix come
  from the registry config.  The rebuilt ref stays bare shorthand form (no
  ``host``/``artifactory_prefix`` set) so identity and lockfile shape are
  preserved.

The resolver is **deterministic**:

* For unambiguous paths (a single plausible owner/repo split), it returns the
  parse-time dependency reference unchanged.
* For ambiguous paths it walks candidate splits shallow-first and locks in the
  first one whose archive responds 2xx-3xx.  No "best-effort fallback" -- if
  every candidate is rejected by the proxy, the function raises and the
  install pipeline surfaces a clear error (and distinguishes "missing repo"
  from "auth problem").

Drift from this contract has historically masked broken deps by silently
falling back to a guess.  Keep the resolver definite.
"""

from __future__ import annotations

import dataclasses

import requests

from apm_cli.core.auth import AuthResolver
from apm_cli.models.apm_package import DependencyReference
from apm_cli.utils.github_host import (
    build_artifactory_archive_url,
    iter_artifactory_boundary_candidates,
    sanitize_token_url_in_message,
)

_ARTIFACTORY_BOUNDARY_UNRESOLVED = (
    "Artifactory host/path did not resolve to a reachable repository archive. "
    "Verify the proxy URL, ref, and that the upstream project exists; the "
    "``//`` notation can mark the repo/virtual boundary explicitly when the "
    "proxy is unavailable."
)

_ARTIFACTORY_BOUNDARY_AUTH = (
    "Artifactory proxy rejected the probe for every candidate boundary "
    "(401/403).  This is an authentication problem, not a missing repo: "
    "check that the configured token has read access to the proxy."
)


class _CandidateStatus:
    """Probe outcome for one candidate boundary."""

    EXISTS = (
        "exists"  # at least one URL shape returned 2xx or 3xx (existence proof, redirect inclusive)
    )
    MISSING = "missing"  # every URL shape returned 4xx other than 401/403
    AUTH = "auth"  # only 401/403 seen -- cannot tell if the repo exists
    INCONCLUSIVE = "inconclusive"  # every URL shape raised a transport error (DNS/TLS/timeout)


def _candidate_archive_status(
    host: str,
    prefix: str,
    owner: str,
    repo: str,
    ref: str,
    headers: dict[str, str],
    verify,
    timeout: int = 15,
    scheme: str = "https",
) -> tuple[str, BaseException | None]:
    """Classify one candidate's existence by HEAD-probing every archive URL shape.

    Returns ``(status, last_exception)`` where ``status`` is one of
    :class:`_CandidateStatus` constants.  Distinguishing ``AUTH`` from
    ``MISSING`` is deliberate: a misconfigured token should surface a
    different error than a wrong owner/repo split.  ``INCONCLUSIVE`` means
    every URL shape raised a transport error (no HTTP response observed) --
    the resolver fails closed on this instead of silently locking in a wrong
    boundary; the last exception is surfaced so the user sees the real
    underlying network issue.
    """
    urls = build_artifactory_archive_url(host, prefix, owner, repo, ref, scheme=scheme)
    saw_auth = False
    saw_any_response = False
    last_exc: BaseException | None = None
    for url in urls:
        try:
            # ``allow_redirects=False`` keeps the Bearer token from leaking to
            # any host the proxy might redirect us to.  3xx is still existence
            # proof -- the server confirmed the resource by issuing a redirect.
            r = requests.head(
                url, headers=headers, timeout=timeout, verify=verify, allow_redirects=False
            )
        except requests.RequestException as exc:
            last_exc = exc
            continue
        saw_any_response = True
        if 200 <= r.status_code < 400:
            return _CandidateStatus.EXISTS, None
        if r.status_code in (401, 403):
            saw_auth = True
    if not saw_any_response:
        return _CandidateStatus.INCONCLUSIVE, last_exc
    if saw_auth:
        return _CandidateStatus.AUTH, None
    return _CandidateStatus.MISSING, None


def _proxy_probe_headers(
    dep_ref: DependencyReference,
    auth_resolver,
    is_mode_1: bool,
) -> dict[str, str]:
    """Build the HEAD-probe headers for the right authentication audience.

    * Mode 1 (explicit FQDN): the dep's host *is* the proxy host, so the
      per-host auth resolver already returns the right token.
    * Mode 2 (bare shorthand under the proxy): the dep's logical host is
      typically ``github.com`` and the auth resolver would hand back a
      GitHub token -- wrong audience.  Use the proxy's own bearer token
      from :class:`RegistryConfig` instead.
    """
    headers: dict[str, str] = {"User-Agent": "apm-cli"}
    if is_mode_1:
        ctx = auth_resolver.resolve_for_dep(dep_ref)
        if ctx and getattr(ctx, "token", None):
            headers["Authorization"] = f"Bearer {ctx.token}"
        return headers
    # Mode 2: proxy-scoped token only.
    from ..deps.registry_proxy import RegistryConfig

    cfg = RegistryConfig.from_env()
    if cfg is not None:
        headers.update(cfg.get_headers())
    return headers


def _proxy_routing_target(
    dep_ref: DependencyReference,
) -> tuple[str, str, str, bool] | None:
    """Return ``(host, prefix, scheme, is_mode_1)`` if *dep_ref* routes through the proxy.

    * Mode 1 (explicit FQDN): host and prefix come from the dependency ref;
      scheme is always ``https`` (Mode 1 deps reject ``http://`` upstream).
    * Mode 2 (bare shorthand under ``PROXY_REGISTRY_ONLY``): host, prefix,
      and scheme come from the registry-proxy config, so installs that
      intentionally route through an ``http://`` proxy (isolated networks
      using ``PROXY_REGISTRY_ALLOW_HTTP=1``) probe over the same transport.

    Returns ``None`` when no proxy routing applies (regular GitHub/ADO deps)
    or when host/prefix are not real strings (e.g. mocked dependency refs
    in unit tests).
    """
    if dep_ref.is_artifactory():
        host = dep_ref.host
        prefix = dep_ref.artifactory_prefix
        if isinstance(host, str) and isinstance(prefix, str) and host and prefix:
            return (host, prefix, "https", True)
        return None
    from ..deps.artifactory_orchestrator import ArtifactoryRouter

    if not ArtifactoryRouter.should_use_proxy(dep_ref):
        return None
    cfg = ArtifactoryRouter.parse_proxy_config()
    if cfg is None:
        return None
    host, prefix, scheme = cfg
    if isinstance(host, str) and isinstance(prefix, str) and host and prefix:
        return (host, prefix, scheme or "https", False)
    return None


def _resolve_artifactory_boundary(
    package: str,
    auth_resolver,
    verbose: bool = False,
    *,
    dep_ref: DependencyReference | None = None,
    logger=None,
) -> DependencyReference:
    """Definitively resolve the (owner, repo, virtual_path) boundary on the proxy.

    Returns the rebuilt :class:`DependencyReference` with the proxy-verified
    boundary, or *dep_ref* unchanged when there is nothing to disambiguate
    (single candidate, or the dep doesn't route through the proxy at all).
    Raises ``ValueError`` if every candidate is rejected by the proxy.
    """
    if auth_resolver is None:
        auth_resolver = AuthResolver()

    if dep_ref is None:
        dep_ref = DependencyReference.parse(package)

    target = _proxy_routing_target(dep_ref)
    if target is None:
        # Not routed through the proxy -- nothing for this resolver to do.
        return dep_ref
    host, prefix, scheme, is_mode_1 = target

    # Strip any inlined ``user:pass@host`` credentials before echoing the
    # package string back in error messages.  Deferred until after the
    # routing-target check so non-proxy deps short-circuit before touching
    # the host (callers occasionally pass mocked dep_refs whose ``host`` is
    # not a real string).
    safe_package = sanitize_token_url_in_message(package, host=host)

    prefix_segs = prefix.split("/")
    tail = dep_ref.repo_url or ""
    if dep_ref.virtual_path:
        tail = f"{tail}/{dep_ref.virtual_path}"
    tail_segs = [s for s in tail.split("/") if s]
    path_segments = [*prefix_segs, *tail_segs]

    candidates = list(iter_artifactory_boundary_candidates(path_segments))
    if not candidates:
        raise ValueError(f"Artifactory dep '{safe_package}' has no plausible owner/repo split")
    if len(candidates) == 1:
        # Single candidate -- the parse-time dep_ref is already definitive.
        return dep_ref

    headers = _proxy_probe_headers(dep_ref, auth_resolver, is_mode_1)
    verify = True

    ref = dep_ref.reference or "main"

    all_auth = True
    last_inconclusive_exc: BaseException | None = None
    for cand_prefix, cand_owner, cand_repo, cand_virtual in candidates:
        if verbose:
            path_suffix = f" [path: {cand_virtual}]" if cand_virtual else ""
            probe_msg = (
                f"  artifactory-resolve: probing {host}/{cand_prefix}/{cand_owner}"
                f"/{cand_repo}#{ref}{path_suffix}"
            )
            # Route through CommandLogger when available so verbose output
            # honors the shared console/theme path (verbose_detail self-gates
            # on logger.verbose). Fall back to print for callers that pass
            # verbose=True without a logger (e.g. legacy unit tests).
            if logger is not None and hasattr(logger, "verbose_detail"):
                logger.verbose_detail(probe_msg)
            else:
                print(probe_msg)
        status, exc = _candidate_archive_status(
            host, cand_prefix, cand_owner, cand_repo, ref, headers, verify, scheme=scheme
        )
        if status == _CandidateStatus.EXISTS:
            # If the probe confirms the parse-time boundary, return the
            # original ref so the install pipeline doesn't re-serialize
            # a structurally unchanged dep as a different shape.
            if (
                dep_ref.repo_url == f"{cand_owner}/{cand_repo}"
                and (dep_ref.virtual_path or None) == cand_virtual
            ):
                return dep_ref
            return _rebuild_dep_ref(
                dep_ref, host, cand_prefix, cand_owner, cand_repo, cand_virtual, is_mode_1
            )
        if status == _CandidateStatus.MISSING:
            all_auth = False
        elif status == _CandidateStatus.INCONCLUSIVE:
            # Transport error on every URL shape (DNS / TLS / timeout) means
            # we don't actually know whether the candidate exists -- fail
            # closed so a wrong boundary can't be silently anchored.
            all_auth = False
            if exc is not None:
                last_inconclusive_exc = exc

    # Every candidate was rejected.  Surface the most-specific failure mode
    # the user can act on: a network outage that prevented any probe from
    # completing dominates the "missing repo" vs "auth problem" choice.
    if last_inconclusive_exc is not None:
        raise ValueError(
            f"Artifactory boundary probe could not reach the proxy for any "
            f"candidate (last error: {last_inconclusive_exc}). "
            f"Verify network reachability and TLS trust to {host}; the ``//`` "
            f"notation can mark the repo/virtual boundary explicitly when the "
            f"proxy is unavailable. (package: {safe_package})"
        )
    if all_auth:
        raise ValueError(f"{_ARTIFACTORY_BOUNDARY_AUTH} (package: {safe_package})")
    raise ValueError(f"{_ARTIFACTORY_BOUNDARY_UNRESOLVED} (package: {safe_package})")


def _rebuild_dep_ref(
    dep_ref: DependencyReference,
    host: str,
    prefix: str,
    owner: str,
    repo: str,
    virtual_path: str | None,
    is_mode_1: bool,
) -> DependencyReference:
    """Rebuild *dep_ref* at the probed boundary, preserving non-boundary fields.

    Mode 1 keeps the FQDN+prefix on the rebuilt ref (it was there to begin
    with).  Mode 2 keeps the rebuilt ref as bare shorthand so identity and
    lockfile shape stay stable -- the proxy is still routed via env, not via
    embedded host/prefix.
    """
    if is_mode_1:
        return DependencyReference.from_artifactory_boundary_probe(
            host, prefix, owner, repo, virtual_path, dep_ref.reference
        )
    return dataclasses.replace(
        dep_ref,
        repo_url=f"{owner}/{repo}",
        virtual_path=virtual_path,
        is_virtual=bool(virtual_path),
    )
