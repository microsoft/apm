"""Explicit policy source for ``apm install`` / ``apm update`` (#1925).

``--policy <path|url|owner/repo>`` names a policy that applies in addition to
auto-discovery. It serves remotes discovery cannot reach (plain git, other
hosts) and air-gapped installs (a local path).

This module owns two decisions:

* the process-scoped explicit source a command opens with
  :func:`explicit_policy_scope` and discovery reads with
  :func:`active_explicit_policy_source`, so every install-time policy reader
  (dependency gate, preflights, executable trust, lifecycle scripts) sees the
  same effective policy; and
* :func:`compose_explicit_policy`, which combines the discovered result with
  the explicit one through tighten-only inheritance (the explicit source can
  add constraints, never remove them). A source that cannot be loaded is kept
  in ``unavailable_sources``: outcome routing fails closed on the named
  ``--policy`` source whatever ``policy.fetch_failure_default`` says, and
  applies that default only to a failed discovered policy.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-checking only
    from .discovery import PolicyFetchResult

_LOCK = threading.RLock()
_active_source: str | None = None


def normalize_explicit_policy_source(source: str | None) -> str | None:
    """Return the canonical explicit source, or ``None`` for discovery only.

    ``org`` (and an empty value) means auto-discovery, which always runs, so
    it adds no explicit source. A local path is made absolute so a later
    working-directory change (``--root``) cannot retarget it.
    """
    if not source or source.strip() in ("", "org"):
        return None
    source = source.strip()
    from .discovery import _expand_policy_path, _is_unambiguous_local_policy_path

    path = _expand_policy_path(source)
    if path.is_file() or _is_unambiguous_local_policy_path(source):
        return str(path.resolve())
    return source


@contextlib.contextmanager
def explicit_policy_scope(source: str | None) -> Iterator[str | None]:
    """Bind an explicit policy source to one command invocation.

    Opened once per invocation by the shared ``--policy`` option of
    ``apm install`` and ``apm update`` (``commands/_helpers.py``).
    Yields the normalized source (``None`` when discovery alone applies).
    """
    global _active_source
    normalized = normalize_explicit_policy_source(source)
    with _LOCK:
        previous = _active_source
        _active_source = normalized
    try:
        yield normalized
    finally:
        with _LOCK:
            _active_source = previous


def active_explicit_policy_source() -> str | None:
    """Return the explicit policy source bound to this invocation, if any."""
    with _LOCK:
        return _active_source


def explicit_source_needs_standalone_check(apm_package) -> bool:
    """True when a named ``--policy`` source is active and no APM dependency
    gate will load it (no APM dependencies): the caller must route it alone."""
    return active_explicit_policy_source() is not None and not (
        apm_package.has_any_apm_dependencies()
    )


def _as_unavailable(result: PolicyFetchResult, label: str) -> PolicyFetchResult:
    """Project a component that produced no policy into a routed failure.

    The label is credential-free: it is printed and raised as-is.
    """
    from .discovery import _redact_policy_ref
    from .outcome_routing import POLICY_NOT_PUBLISHED_OUTCOMES, classify_discovery_outcome

    source = _redact_policy_ref(result.source) if result.source else label
    if classify_discovery_outcome(result, fetch_failure_default="warn").unresolved:
        # A resolution failure keeps its outcome and fail-closed routing.
        return replace(result, source=source)
    reason = result.error or result.fetch_error
    if not reason and result.outcome in POLICY_NOT_PUBLISHED_OUTCOMES:
        # A cache-only (offline) lookup with no cached copy reports no source.
        reason = "no apm-policy.yml found" if result.source else "no cached copy available offline"
    return replace(
        result,
        policy=None,
        outcome="cache_miss_fetch_fail",
        source=source,
        error=reason or result.outcome or "no policy was resolved",
    )


def compose_explicit_policy(
    discovered: PolicyFetchResult,
    resolve: Callable[[str], PolicyFetchResult],
) -> PolicyFetchResult:
    """Combine *discovered* with the active explicit source, if any.

    *resolve* loads one source with its ``extends:`` chain. The project's
    ``policy.hash`` pin covers the discovered org policy, not ``--policy``,
    so *resolve* runs unpinned.

    The discovered policy is the parent and the explicit policy the child of
    a tighten-only merge, so the explicit source can never re-allow what the
    discovered policy denies. A failed component is carried in
    ``unavailable_sources`` (the explicit one marked ``explicit=True``) and
    routed by ``route_discovery_outcome``. A valid stale cached copy counts
    as loaded.
    """
    from . import inheritance
    from .discovery import _is_policy_empty, _redact_policy_ref
    from .outcome_routing import POLICY_NOT_PUBLISHED_OUTCOMES

    source = active_explicit_policy_source()
    if source is None or discovered.outcome == "disabled":
        return discovered
    explicit = resolve(source)
    if explicit.outcome == "disabled":
        return discovered
    if explicit.policy is None:
        # The named source failed: routing blocks on it unconditionally.
        failed = replace(
            _as_unavailable(explicit, f"--policy {_redact_policy_ref(source)}"), explicit=True
        )
        return replace(
            discovered,
            explicit=True,
            unavailable_sources=[*discovered.unavailable_sources, failed],
        )

    if discovered.policy is None:
        failed = []
        if discovered.outcome not in POLICY_NOT_PUBLISHED_OUTCOMES and (
            discovered.outcome or discovered.error
        ):
            failed.append(_as_unavailable(discovered, "org policy discovery"))
        return replace(
            explicit,
            explicit=True,
            unavailable_sources=[*explicit.unavailable_sources, *failed],
        )

    merged = inheritance.merge_policies(discovered.policy, explicit.policy)
    stale = next(
        (r for r in (discovered, explicit) if r.outcome == "cached_stale"),
        None,
    )
    if stale is not None:
        outcome = "cached_stale"
    else:
        outcome = "empty" if _is_policy_empty(merged) else "found"
    return replace(
        discovered,
        policy=merged,
        source=f"{discovered.source} + {explicit.source}",
        outcome=outcome,
        explicit=True,
        cached=discovered.cached and explicit.cached,
        cache_stale=stale is not None,
        cache_age_seconds=stale.cache_age_seconds if stale else None,
        fetch_error=stale.fetch_error if stale else None,
        warnings=[*discovered.warnings, *explicit.warnings],
        unavailable_sources=[*discovered.unavailable_sources, *explicit.unavailable_sources],
    )
