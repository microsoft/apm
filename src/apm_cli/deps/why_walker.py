"""Inverted dependency-graph walker for ``apm deps why``.

Pure data-structure logic over an in-memory :class:`LockFile`. No Click,
no I/O. The walker inverts the existing forward ``resolved_by`` graph
(each :class:`LockedDependency` points at its parent's ``repo_url``) and
collects all paths from a target dependency back to one or more direct
(root) dependencies.

Backward compatible with lockfiles that predate the optional
``constraint`` field on :class:`LockedDependency` (issue #1488). When the
attribute is absent, :class:`WhyEdge.constraint` is ``None`` and human
output falls back to a plain "declared in apm.yml" annotation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .lockfile import LockedDependency, LockFile


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WhyEdge:
    """A single parent -> child edge in a why-explanation chain.

    ``parent_key`` is ``None`` only for the synthetic root edge that
    represents the project's own ``apm.yml`` direct-dependency declaration.
    """

    parent_key: str | None
    child_key: str
    constraint: str | None = None


@dataclass(frozen=True)
class WhyPath:
    """A single root-to-target chain. ``chain[0].parent_key`` is ``None``."""

    chain: tuple[WhyEdge, ...]


@dataclass(frozen=True)
class WhyResult:
    """Result of explaining a target dependency.

    NOTE: ``target`` is a borrowed reference to the same
    :class:`LockedDependency` instance held by the live
    :class:`LockFile`. Treat it as read-only; mutating it would alter
    the lockfile object held by the caller. (Frozen WhyResult only
    freezes its own attributes, not the target it points at.)
    """

    target: LockedDependency
    is_direct: bool
    paths: tuple[WhyPath, ...]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PackageNotInstalledError(Exception):
    """Raised when a query does not match any installed package."""

    def __init__(self, query: str):
        super().__init__(f"'{query}' is not installed (not in apm.lock.yaml)")
        self.query = query


class AmbiguousPackageError(Exception):
    """Raised when a basename query matches multiple installed packages."""

    def __init__(self, query: str, matches: list[str]):
        super().__init__(f"'{query}' matches multiple packages: {', '.join(sorted(matches))}")
        self.query = query
        self.matches = sorted(matches)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dep_constraint(dep: LockedDependency) -> str | None:
    """Return the declared constraint for *dep*, or ``None`` when unknown.

    Reads the optional ``constraint`` attribute introduced by issue #1488.
    Gracefully degrades when the attribute is absent on the dataclass.
    """
    return getattr(dep, "constraint", None)


def _basename(repo_url: str) -> str:
    """Return the trailing path segment of *repo_url* (e.g. ``shared-utils``).

    Strips a trailing ``.git`` for convenience. ``acme-org/shared-utils.git``
    -> ``shared-utils``.
    """
    tail = repo_url.rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _owner_repo(repo_url: str) -> str:
    """Return the last two path segments of *repo_url* (``owner/repo``).

    Falls back to the full ``repo_url`` when fewer than two segments are
    available.
    """
    parts = repo_url.rstrip("/").split("/")
    if len(parts) >= 2:
        tail = parts[-1]
        if tail.endswith(".git"):
            tail = tail[:-4]
        return f"{parts[-2]}/{tail}"
    return repo_url


def _is_project_dep(dep: LockedDependency) -> bool:
    """Filter out the synthesized ``<self>`` entry from traversals."""
    return dep.repo_url != "<self>"


# ---------------------------------------------------------------------------
# Query resolution
# ---------------------------------------------------------------------------


def resolve_package_query(lockfile: LockFile, query: str) -> LockedDependency:
    """Resolve a user query string to a single :class:`LockedDependency`.

    Supports four forms:

    1. Full ``repo_url`` (exact match).
    2. ``owner/repo`` short form.
    3. Bare basename (e.g. ``shared-utils``), if unique.
    4. The unique key returned by ``LockedDependency.get_unique_key()``.

    Raises :class:`PackageNotInstalledError` when nothing matches, and
    :class:`AmbiguousPackageError` when a basename matches multiple
    packages from different owners.
    """
    if not query:
        raise PackageNotInstalledError(query)

    deps = [d for d in lockfile.get_all_dependencies() if _is_project_dep(d)]

    # Exact match on unique key
    by_key = {d.get_unique_key(): d for d in deps}
    if query in by_key:
        return by_key[query]

    # Exact match on repo_url
    by_url = [d for d in deps if d.repo_url == query]
    if len(by_url) == 1:
        return by_url[0]

    # Exact match on owner/repo
    by_owner_repo = [d for d in deps if _owner_repo(d.repo_url) == query]
    if len(by_owner_repo) == 1:
        return by_owner_repo[0]
    if len(by_owner_repo) > 1:
        raise AmbiguousPackageError(query, [d.repo_url for d in by_owner_repo])

    # Bare basename
    by_basename = [d for d in deps if _basename(d.repo_url) == query]
    if len(by_basename) == 1:
        return by_basename[0]
    if len(by_basename) > 1:
        raise AmbiguousPackageError(query, [d.repo_url for d in by_basename])

    raise PackageNotInstalledError(query)


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


def compute_why(lockfile: LockFile, target: LockedDependency) -> WhyResult:
    """Walk the single ``resolved_by`` chain from *target* back to a root.

    :class:`LockedDependency.resolved_by` records exactly one parent
    today, so a target produces ONE chain per lockfile record (any
    "diamond" fan-in is collapsed by the resolver). Returns a
    :class:`WhyResult` whose ``paths`` is a tuple of one or more
    :class:`WhyPath` instances ordered deterministically by their
    stringified chain. The iterative worklist generalises to N paths
    without API change if ``resolved_by`` ever becomes multi-valued
    (see #1488 and the constraint-field follow-on).

    When *target* is itself a direct dependency, the result contains one
    trivial path with ``parent_key=None``.

    Cycles in the lockfile graph (which would indicate a resolver bug,
    not user data, but we defend regardless) are broken by a
    per-traversal visited set: each ``repo_url`` appears at most once
    in a single chain. A defensive ``max_paths`` cap also bounds
    worklist growth in the future multi-parent case.
    """
    target_key = target.repo_url
    target_constraint = _dep_constraint(target)

    # Direct dependency: single trivial path.
    if target.resolved_by is None:
        return WhyResult(
            target=target,
            is_direct=True,
            paths=(
                WhyPath(
                    chain=(
                        WhyEdge(
                            parent_key=None,
                            child_key=target_key,
                            constraint=target_constraint,
                        ),
                    )
                ),
            ),
        )

    # Build a lookup: repo_url -> dep. The lockfile may legitimately host
    # multiple deps with the same repo_url under different virtual_path keys;
    # for parent-chain walking we key on repo_url and pick the canonical
    # (non-virtual, lowest-depth) record where ambiguous.
    by_url: dict[str, LockedDependency] = {}
    for dep in lockfile.get_all_dependencies():
        if not _is_project_dep(dep):
            continue
        existing = by_url.get(dep.repo_url)
        if (
            existing is None
            or (existing.is_virtual and not dep.is_virtual)
            or dep.depth < existing.depth
        ):
            by_url[dep.repo_url] = dep

    # Walk parents iteratively. Each entry in the worklist is
    # (current_dep, chain_so_far_root_first, visited_set).
    paths: list[WhyPath] = []
    initial_edge = WhyEdge(
        parent_key=target.resolved_by,
        child_key=target_key,
        constraint=target_constraint,
    )
    worklist: list[tuple[LockedDependency, tuple[WhyEdge, ...], frozenset[str]]] = [
        (target, (initial_edge,), frozenset({target_key}))
    ]

    # Bound traversal depth as a defensive ceiling against pathological data.
    max_depth = max(64, len(by_url) + 1)
    # Bound the total number of paths returned (defensive against the
    # future multi-parent case where a malformed lockfile could produce
    # factorial fan-in). Today resolved_by is single-valued so this cap
    # is unreachable; it matters once #1488's resolver lands.
    max_paths = 256

    while worklist:
        if len(paths) >= max_paths:
            break
        current, chain, visited = worklist.pop()
        if len(chain) > max_depth:
            # Stop extending this chain; record what we have.
            paths.append(WhyPath(chain=chain))
            continue
        parent_url = current.resolved_by
        if parent_url is None:
            paths.append(WhyPath(chain=chain))
            continue
        parent = by_url.get(parent_url)
        if parent is None:
            # Parent missing from lockfile (corrupt or partial data). Treat
            # the current step as a root and stop extending.
            paths.append(WhyPath(chain=chain))
            continue
        if parent.repo_url in visited:
            # Cycle detected; record the chain up to the cycle boundary.
            paths.append(WhyPath(chain=chain))
            continue
        new_edge = WhyEdge(
            parent_key=parent.resolved_by,
            child_key=parent.repo_url,
            constraint=_dep_constraint(parent),
        )
        new_chain = (new_edge, *chain)
        worklist.append((parent, new_chain, visited | {parent.repo_url}))

    # Deterministic ordering: by the rendered chain string.
    paths.sort(key=lambda p: tuple((e.parent_key or "", e.child_key) for e in p.chain))
    return WhyResult(target=target, is_direct=False, paths=tuple(paths))
