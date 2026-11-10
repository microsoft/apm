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
    """Explain original incoming requirements, never substitute source refs.

    Replacement-aware locks record exact parent keys and every incoming
    replacement request. Legacy locks retain their single repo-url parent.
    Cycles terminate at the repeated owner; at most 256 paths are expanded.
    """
    from .lockfile import lockfile_parent_identity

    def key_for(dep: LockedDependency) -> str:
        return lockfile_parent_identity(dep, lockfile.replacements)

    def incoming(dep: LockedDependency) -> tuple[WhyEdge, ...]:
        key = key_for(dep)
        if dep.replacement is not None:
            return tuple(
                WhyEdge(parent_key=request.parent, child_key=key, constraint=request.ref)
                for request in dep.replacement.requests
            )
        return (
            WhyEdge(parent_key=dep.resolved_by, child_key=key, constraint=_dep_constraint(dep)),
        )

    by_key: dict[str, LockedDependency] = {}
    for dep in lockfile.get_all_dependencies():
        if not _is_project_dep(dep):
            continue
        key = key_for(dep)
        existing = by_key.get(key)
        if (
            existing is None
            or (existing.is_virtual and not dep.is_virtual)
            or dep.depth < existing.depth
        ):
            by_key[key] = dep

    target_key = key_for(target)
    target_edges = incoming(target)
    paths: list[WhyPath] = []
    worklist: list[tuple[tuple[WhyEdge, ...], frozenset[str]]] = [
        ((edge,), frozenset({target_key})) for edge in target_edges
    ]
    max_paths = 256
    max_depth = max(64, len(by_key) + 1)
    while worklist:
        if len(paths) >= max_paths:
            break
        chain, visited = worklist.pop()
        parent_key = chain[0].parent_key
        parent = by_key.get(parent_key) if parent_key is not None else None
        if parent is None or parent_key in visited or len(chain) > max_depth:
            paths.append(WhyPath(chain=chain))
            continue
        for edge in incoming(parent):
            worklist.append(((edge, *chain), visited | {parent_key}))

    paths.sort(key=lambda p: tuple((e.parent_key or "", e.child_key) for e in p.chain))
    return WhyResult(
        target=target,
        is_direct=any(edge.parent_key is None for edge in target_edges),
        paths=tuple(paths),
    )
