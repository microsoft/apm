"""Chain-resolution and host-pin helpers for policy discovery.

Leaf module -- does NOT import ``discovery.py`` at module scope.
Back-references to ``discovery`` symbols (``urlparse``,
``_extract_org_from_git_remote``, ``discover_policy``, ``_write_cache``)
use Rule-B function-local imports so that test patches applied via
``apm_cli.policy.discovery.<NAME>`` are still honoured.

Rule-B routing table (all inside ``from apm_cli.policy import discovery as _d``):
- ``_derive_leaf_host``: ``_d.urlparse`` (6 patches), ``_d._extract_org_from_git_remote`` (13 patches)
- ``_extract_extends_host``: ``_d.urlparse`` (6 patches)
- ``_fetch_chain_parent``: ``_d.discover_policy`` (20 patches), ``_d._fetch_from_ado_repo``, ``_d.is_azure_devops_hostname``
- ``_resolve_and_persist_chain``: delegates parent fetch to ``_fetch_chain_parent``, ``_d._write_cache`` (22 patches)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .schema import ApmPolicy


# ---------------------------------------------------------------------------
# Source-label helpers
# ---------------------------------------------------------------------------


def _strip_source_prefix(src: str) -> str:
    """Strip 'org:' / 'url:' / 'file:' prefix from a PolicyFetchResult.source."""
    return src.removeprefix("org:").removeprefix("url:").removeprefix("file:")


def _derive_leaf_host(source: str, project_root: Path) -> str | None:
    """Derive the origin host of the leaf policy.

    The leaf host pins which host an ``extends:`` reference may resolve
    against (Security Finding F1 -- prevents credential leakage to
    attacker-controlled hosts via cross-host extends chains).

    Returns the host in lowercase, or None if it cannot be determined.

    Source forms:
    * ``url:https://<host>/...`` -> ``<host>``
    * ``org:<host>/<owner>/<repo>`` (3+ slash-segments) -> ``<host>``
    * ``org:<owner>/<repo>`` (2 slash-segments) -> ``github.com`` (default)
    * ``file:<path>`` -> fall back to git remote of *project_root*
    """
    # Rule B: import discovery so urlparse and _extract_org_from_git_remote
    # are looked up in the discovery module's namespace at call time, making
    # test patches on apm_cli.policy.discovery.urlparse / ._extract_org_...
    # visible here too.
    from apm_cli.policy import discovery as _d

    if not source:  # noqa: SIM108
        bare = ""
    else:
        bare = _strip_source_prefix(source)

    if source.startswith("url:") or bare.startswith("https://") or bare.startswith("http://"):
        try:
            parsed = _d.urlparse(bare)
            if parsed.hostname:
                return parsed.hostname.lower()
        except Exception:
            return None
        return None

    if source.startswith("org:") or (bare and "://" not in bare and bare.count("/") >= 1):
        parts = bare.split("/")
        if len(parts) >= 3:
            return parts[0].lower()
        if len(parts) == 2:
            # owner/repo shorthand defaults to github.com (matches
            # _fetch_github_contents convention).
            return "github.com"

    # File source (or unrecognized): fall back to project's git remote.
    org_and_host = _d._extract_org_from_git_remote(project_root)
    if org_and_host is not None:
        _, host = org_and_host
        if host:
            return host.lower()
    return None


def _extract_extends_host(ref: str) -> str | None:
    """Return the host an ``extends:`` ref resolves against, if explicit.

    * Full URL -> URL host (lowercase)
    * ``<host>/<owner>/<repo>`` (3+ slash-segments) -> ``<host>`` (lowercase)
    * ``<owner>/<repo>`` shorthand -> None (intrinsically same-host)
    * ``<org>`` shorthand (no slash) -> None (intrinsically same-host)
    """
    # Rule B: use _d.urlparse so test patches on discovery.urlparse apply.
    from apm_cli.policy import discovery as _d

    if not ref:
        return None
    if ref.startswith("http://") or ref.startswith("https://"):
        try:
            parsed = _d.urlparse(ref)
            if parsed.hostname:
                return parsed.hostname.lower()
        except Exception:
            return None
        return None
    if "/" not in ref:
        return None
    parts = ref.split("/")
    if len(parts) >= 3:
        return parts[0].lower()
    return None


def _validate_extends_host(leaf_host: str | None, extends_ref: str) -> None:
    """Reject ``extends:`` refs that point at a different host than the leaf.

    Raises :class:`PolicyInheritanceError` (imported lazily to avoid a
    module-level cycle) when the ``extends:`` ref names a host that does
    not match *leaf_host*. Pure shorthand refs (``owner/repo``, ``org``)
    are intrinsically same-host and always pass.

    See Security Finding F1: a malicious org policy author setting
    ``extends: "evil.example.com/org/.github"`` could otherwise route
    ``git credential fill`` against an attacker-controlled host.
    """
    from . import inheritance as _inheritance_mod

    extends_host = _extract_extends_host(extends_ref)
    if extends_host is None:
        return  # shorthand: intrinsically same-host, allowed.

    if leaf_host is None:
        raise _inheritance_mod.PolicyInheritanceError(
            f"Policy extends: cross-host reference rejected "
            f"(leaf host: <unknown>, extends host: {extends_host}); "
            f"cross-host policy chains are not allowed"
        )

    if extends_host != leaf_host.lower():
        raise _inheritance_mod.PolicyInheritanceError(
            f"Policy extends: cross-host reference rejected "
            f"(leaf host: {leaf_host}, extends host: {extends_host}); "
            f"cross-host policy chains are not allowed"
        )


def _resolve_ado_parent_ref(
    parent_ref: str,
    current_source: str,
    leaf_host: str,
) -> tuple[str, str, str, str] | None:
    """Normalise an ADO parent ref to ``(org, project, repo, host)``."""
    from apm_cli.policy import discovery as _d

    from ..utils.github_host import is_visualstudio_legacy_hostname

    current_parts = _strip_source_prefix(current_source).split("/")
    if is_visualstudio_legacy_hostname(leaf_host):
        current_org = leaf_host[: -len(".visualstudio.com")]
    elif len(current_parts) >= 2:
        current_org = current_parts[1]
    else:
        current_org = ""

    if parent_ref == "org":
        resolved = (current_org, _d.ADO_POLICY_PROJECT, "_apm", leaf_host)
    else:
        parts = parent_ref.strip("/").split("/")
        explicit_host = _extract_extends_host(parent_ref)
        if explicit_host is None and len(parts) == 2:
            resolved = (current_org, parts[0], parts[1], leaf_host)
        elif explicit_host and is_visualstudio_legacy_hostname(explicit_host) and len(parts) >= 3:
            resolved = (
                explicit_host[: -len(".visualstudio.com")],
                parts[1],
                "/".join(parts[2:]),
                explicit_host,
            )
        elif explicit_host and _d.is_azure_devops_hostname(explicit_host) and len(parts) >= 4:
            resolved = (
                parts[1],
                parts[2],
                "/".join(parts[3:]),
                explicit_host,
            )
        else:
            return None

    return resolved if all(resolved[:3]) else None


def _fetch_chain_parent(
    parent_ref: str,
    *,
    current_source: str,
    leaf_host: str | None,
    project_root: Path,
    no_cache: bool,
    cache_only: bool = False,
) -> object:
    """Fetch one parent without losing the leaf host or backend.

    GitHub-style ``owner/repo`` refs are qualified with a non-default leaf
    host. On ADO, ``project/repo`` means a repo in the current ancestor's
    org, while explicit refs use ``host/org/project/repo`` on
    ``dev.azure.com`` or ``host/project/repo`` on legacy
    ``*.visualstudio.com`` hosts. URLs and local-file refs continue through
    the public single-policy owner.

    Returns a :class:`apm_cli.policy.discovery.PolicyFetchResult`.
    """
    from pathlib import Path as _Path

    from apm_cli.policy import discovery as _d

    cache_kwargs = {"cache_only": True} if cache_only else {}
    if parent_ref.startswith(("http://", "https://")) or _Path(parent_ref).is_file():
        return _d.discover_policy(
            project_root,
            policy_override=parent_ref,
            no_cache=no_cache,
            **cache_kwargs,
        )

    if leaf_host and _d.is_azure_devops_hostname(leaf_host):
        resolved = _resolve_ado_parent_ref(parent_ref, current_source, leaf_host)
        if resolved is None:
            return _d.PolicyFetchResult(
                source=f"org:{parent_ref}",
                error=f"Invalid Azure DevOps policy reference: {parent_ref}",
                outcome="cache_miss_fetch_fail",
            )
        org, project, repo, host = resolved
        return _d._fetch_from_ado_repo(
            org=org,
            project=project,
            repo=repo,
            host=host,
            project_root=project_root,
            no_cache=no_cache,
            **cache_kwargs,
        )

    normalized_ref = parent_ref
    if leaf_host and leaf_host != "github.com" and _extract_extends_host(parent_ref) is None:
        if "/" in parent_ref:
            normalized_ref = f"{leaf_host}/{parent_ref}"
    return _d.discover_policy(
        project_root,
        policy_override=normalized_ref,
        no_cache=no_cache,
        **cache_kwargs,
    )


def _resolve_and_persist_chain(
    fetch_result: object,  # PolicyFetchResult
    project_root: Path,
    *,
    no_cache: bool = False,
    cache_only: bool = False,
) -> None:
    """Resolve inheritance chain and update cache with merged policy + chain_refs.

    Walks the ``extends:`` chain depth-first, fetching each parent via
    :func:`_fetch_chain_parent`.  Cycle detection on normalised ``extends:``
    refs and ``MAX_CHAIN_DEPTH`` enforcement protect against runaway or
    self-referential chains.

    Fail-closed on incomplete chains: if any parent fetch fails, the partial
    chain is NOT merged -- ``fetch_result.policy`` is set to ``None`` and
    ``fetch_result.outcome`` is ``"incomplete_chain"`` so enforcement cannot
    proceed with weaker constraints.

    Mutates *fetch_result* in-place.
    Called by :func:`discover_policy_with_chain` -- not intended for direct use.
    """
    # Rule B: _write_cache is patched via apm_cli.policy.discovery._write_cache
    # in tests; look it up via _d so patches apply.
    from apm_cli.policy import discovery as _d

    from . import inheritance as _inheritance_mod

    leaf_policy = fetch_result.policy
    leaf_source = fetch_result.source

    # Host pin: extends: refs may only resolve against the leaf's origin host.
    # Prevents credential leakage to attacker-controlled hosts via cross-host
    # extends chains (Security Finding F1).
    leaf_host = _derive_leaf_host(leaf_source, project_root)

    # Ordered ancestors collected as we walk parents.  Built leaf-first for
    # traversal convenience; reversed before merging.
    chain_policies: list[ApmPolicy] = [leaf_policy]
    chain_sources: list[str] = [leaf_source]

    # Track normalised refs we've already followed to break cycles.
    # We seed with the leaf's source so an extends pointing back at the leaf
    # is also detected.
    visited: list[str] = [_strip_source_prefix(leaf_source)] if leaf_source else []

    current = leaf_policy
    incomplete_chain: tuple[str, int, int] | None = None
    stale_ancestor: object | None = None  # PolicyFetchResult | None

    while current.extends:
        next_ref = current.extends

        # Host pin enforcement: must validate BEFORE any fetch so we never call
        # git credential fill against an attacker-controlled host.
        _validate_extends_host(leaf_host, next_ref)

        if _inheritance_mod.detect_cycle(visited, next_ref):
            raise _inheritance_mod.PolicyInheritanceError(
                f"Cycle detected in policy extends chain: {' -> '.join(visited)} -> {next_ref}"
            )

        # Depth check: chain_policies already has len() entries; next fetch
        # would push us to len()+1.  resolve_policy_chain enforces this
        # afterwards, but failing here gives a clearer error.
        if len(chain_policies) + 1 > _inheritance_mod.MAX_CHAIN_DEPTH:
            raise _inheritance_mod.PolicyInheritanceError(
                f"Policy chain depth exceeds maximum of "
                f"{_inheritance_mod.MAX_CHAIN_DEPTH} "
                f"(chain: {' -> '.join(visited)} -> {next_ref})"
            )

        parent_result = _fetch_chain_parent(
            next_ref,
            current_source=chain_sources[-1],
            leaf_host=leaf_host,
            project_root=project_root,
            no_cache=no_cache,
            cache_only=cache_only,
        )
        fetch_result.warnings.extend(parent_result.warnings)

        if parent_result.policy is None:
            # Parent fetch failed -- never enforce the weaker partial chain.
            attempted = len(chain_policies) + 1
            resolved = len(chain_policies)
            incomplete_chain = (next_ref, resolved, attempted)
            break

        # Keep the nearest stale ancestor's refresh diagnostic.  Continue
        # merging its usable cached policy, but never relabel that ancestry as
        # fresh or replace a nearer failure with a farther one.
        if parent_result.outcome == "cached_stale" and stale_ancestor is None:
            stale_ancestor = parent_result

        chain_policies.append(parent_result.policy)
        chain_sources.append(parent_result.source)
        visited.append(next_ref)
        current = parent_result.policy

    # No actual ancestors fetched -- nothing to merge or re-cache.
    if len(chain_policies) == 1:
        if incomplete_chain is not None:
            ref, resolved, attempted = incomplete_chain
            fetch_result.outcome = "incomplete_chain"
            fetch_result.error = (
                f"Policy chain incomplete: {ref} unreachable "
                f"({resolved} of {attempted} policies resolved)"
            )
            fetch_result.policy = None
        return

    # Merge in [root, ..., leaf] order.  We collected leaf-first, so reverse.
    ordered = list(reversed(chain_policies))
    ordered_sources = list(reversed(chain_sources))

    try:
        merged = _inheritance_mod.resolve_policy_chain(ordered)
    except _inheritance_mod.PolicyInheritanceError:
        # Re-raise depth errors from the canonical validator so callers see a
        # single consistent error type.
        raise

    chain_refs: list[str] = [_strip_source_prefix(src) for src in ordered_sources if src]

    cache_key = _strip_source_prefix(leaf_source) if leaf_source else ""
    if cache_key and incomplete_chain is None and stale_ancestor is None:
        _d._write_cache(
            cache_key,
            merged,
            project_root,
            chain_refs=chain_refs,
            raw_bytes_hash=fetch_result.raw_bytes_hash,
            warnings=fetch_result.warnings,
        )

    if incomplete_chain is not None:
        ref, resolved, attempted = incomplete_chain
        fetch_result.outcome = "incomplete_chain"
        fetch_result.error = (
            f"Policy chain incomplete: {ref} unreachable "
            f"({resolved} of {attempted} policies resolved)"
        )
        fetch_result.policy = None
        return

    fetch_result.policy = merged
    if stale_ancestor is not None:
        fetch_result.outcome = "cached_stale"
        fetch_result.fetch_error = stale_ancestor.fetch_error or stale_ancestor.error
        fetch_result.cached = True
        fetch_result.cache_stale = True
        fetch_result.cache_age_seconds = stale_ancestor.cache_age_seconds
        return
    fetch_result.outcome = "empty" if _d._is_policy_empty(merged) else "found"
