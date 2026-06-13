"""Chain-resolution and host-pin helpers for policy discovery.

Leaf module -- does NOT import ``discovery.py`` at module scope.
Back-references to ``discovery`` symbols (``urlparse``,
``_extract_org_from_git_remote``, ``discover_policy``, ``_write_cache``)
use Rule-B function-local imports so that test patches applied via
``apm_cli.policy.discovery.<NAME>`` are still honoured.

Rule-B routing table (all inside ``from apm_cli.policy import discovery as _d``):
- ``_derive_leaf_host``: ``_d.urlparse`` (6 patches), ``_d._extract_org_from_git_remote`` (13 patches)
- ``_extract_extends_host``: ``_d.urlparse`` (6 patches)
- ``_resolve_and_persist_chain``: ``_d.discover_policy`` (20 patches), ``_d._write_cache`` (22 patches)
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


def _resolve_and_persist_chain(
    fetch_result: object,  # PolicyFetchResult
    project_root: Path,
) -> None:
    """Resolve inheritance chain and update cache with merged policy + chain_refs.

    Walks the ``extends:`` chain depth-first, fetching each parent via the
    single-policy ``discover_policy`` (so each fetch still hits the
    well-tested fetch path).  Cycle detection on normalized ``extends:``
    refs and ``MAX_CHAIN_DEPTH`` enforcement protect against runaway or
    self-referential chains.

    Partial-chain policy: if any parent fetch fails, emit a warning via
    ``_rich_warning`` and merge whatever was resolved so far -- never
    silently drop ancestors.

    Mutates *fetch_result*.policy in-place with the merged effective policy.
    Called by :func:`discover_policy_with_chain` -- not intended for direct
    use.
    """
    # Rule B: discover_policy and _write_cache are patched via
    # apm_cli.policy.discovery.* in tests; look them up via _d so patches apply.
    from apm_cli.policy import discovery as _d

    from ..utils.console import _rich_warning
    from . import inheritance as _inheritance_mod

    leaf_policy = fetch_result.policy
    leaf_source = fetch_result.source

    # Host pin: extends: refs may only resolve against the leaf's origin
    # host. Prevents credential leakage to attacker-controlled hosts via
    # cross-host extends chains (Security Finding F1).
    leaf_host = _derive_leaf_host(leaf_source, project_root)

    # Ordered ancestors collected as we walk parents.  Built leaf-first
    # for traversal convenience; reversed before merging.
    chain_policies: list[ApmPolicy] = [leaf_policy]
    chain_sources: list[str] = [leaf_source]

    # Track normalized refs we've already followed to break cycles.
    # We seed with the leaf's source so an extends pointing back at the
    # leaf is also detected.
    visited: list[str] = [_strip_source_prefix(leaf_source)] if leaf_source else []

    current = leaf_policy
    partial_warning: tuple[str, int, int] | None = None

    while current.extends:
        next_ref = current.extends

        # Host pin enforcement: must validate BEFORE any fetch so we never
        # call git credential fill against an attacker-controlled host.
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

        parent_result = _d.discover_policy(
            project_root,
            policy_override=next_ref,
            no_cache=False,
        )

        if parent_result.policy is None:
            # Parent fetch failed -- merge what we have so far and warn.
            attempted = len(chain_policies) + 1
            resolved = len(chain_policies)
            partial_warning = (next_ref, resolved, attempted)
            break

        chain_policies.append(parent_result.policy)
        chain_sources.append(parent_result.source)
        visited.append(next_ref)
        current = parent_result.policy

    # No actual ancestors fetched -- nothing to merge or re-cache.
    if len(chain_policies) == 1:
        if partial_warning is not None:
            ref, resolved, attempted = partial_warning
            _rich_warning(
                f"Policy chain incomplete: {ref} unreachable, "
                f"using {resolved} of {attempted} policies",
                symbol="warning",
            )
        return

    # Merge in [root, ..., leaf] order.  We collected leaf-first, so reverse.
    ordered = list(reversed(chain_policies))
    ordered_sources = list(reversed(chain_sources))

    try:
        merged = _inheritance_mod.resolve_policy_chain(ordered)
    except _inheritance_mod.PolicyInheritanceError:
        # Re-raise depth errors from the canonical validator so callers
        # see a single consistent error type.
        raise

    chain_refs: list[str] = [_strip_source_prefix(src) for src in ordered_sources if src]

    cache_key = _strip_source_prefix(leaf_source) if leaf_source else ""
    if cache_key:
        _d._write_cache(cache_key, merged, project_root, chain_refs=chain_refs)

    fetch_result.policy = merged

    if partial_warning is not None:
        ref, resolved, attempted = partial_warning
        _rich_warning(
            f"Policy chain incomplete: {ref} unreachable, using {resolved} of {attempted} policies",
            symbol="warning",
        )
