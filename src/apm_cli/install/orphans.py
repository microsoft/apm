"""Orphan removal decisions for every command (#3879, #4011).

Single owner of which locked packages, installed ``apm_modules/`` roots and
deployed files a command removes because nothing retained needs them any
more. ``apm install`` (full, targeted and ``--dry-run``), ``apm prune``,
``apm uninstall`` (and their dry runs), ``apm audit --ci`` (no-orphans),
``apm deps list`` and the compile advisory all call
:func:`decide_orphan_removal`; their differences are explicit inputs, never
private predicates. Forward reachability stays with
:mod:`apm_cli.deps.reachability`, which this module consumes. File deletion
stays with the cleanup owner (``integration/cleanup.py``) and the commands'
deletion loops; a decision is read-only and a dry run renders the same
decision the real run acts on.

Everything except the public names in ``__all__`` is private to this module;
the architecture linter rejects any other reference, alias or ``getattr``
read from product code, and any entry-point call that omits an input.
"""

from __future__ import annotations

import enum
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from ..deps.lockfile import _SELF_KEY, LockFile, lockfile_parent_identity
from ..models.dependency.materialization import (
    CachedMaterializationPathReader,
    installed_package_path,
)
from ..models.dependency.selection import parse_dependency_entry
from ..utils.path_security import PathTraversalError, validate_path_segments

if TYPE_CHECKING:
    from ..deps.reachability import ReachabilityResult
    from ..models.dependency.reference import DependencyReference

__all__ = [
    "DeploymentClaims",
    "InstalledPaths",
    "OrphanOperation",
    "OrphanRemovalDecision",
    "ReachabilityProbe",
    "decide_orphan_removal",
]


class OrphanOperation(enum.Enum):
    """The command whose removal decision is requested.

    A preview and its real run pass the same operation and inputs, so they
    always receive the same decision.
    """

    INSTALL = "install"
    TARGETED_INSTALL = "targeted-install"
    PRUNE = "prune"
    UNINSTALL = "uninstall"
    AUDIT = "audit"
    INSPECT = "inspect"


@dataclass(frozen=True)
class ReachabilityProbe:
    """Inputs for the forward walk :mod:`apm_cli.deps.reachability` performs.

    The walk reports its own completeness; an incomplete walk keeps every
    candidate it was asked about.
    """

    project_root: Path
    apm_modules_dir: Path
    direct_refs: tuple[DependencyReference, ...]
    dev_refs: tuple[DependencyReference, ...]


@dataclass(frozen=True)
class InstalledPaths:
    """The ``apm_modules`` root and, when scanned, its installed package paths."""

    root: Path
    packages: frozenset[str] | None


@dataclass(frozen=True)
class DeploymentClaims:
    """Deployed-file evidence that keeps removal from touching live content.

    ``live_files`` are paths a retained owner still deploys; ``None`` means
    offline evidence: the retained lock rows' recorded claims. ``retained``
    maps each candidate key to files the cleanup owner could not delete, so
    the package keeps its lock row (and its checkout) for a later retry.
    """

    live_files: frozenset[str] | None
    retained: Mapping[str, Mapping[str, str | None]]


@dataclass(frozen=True)
class OrphanRemovalDecision:
    """One command's complete, read-only removal decision.

    ``removed_keys`` are the lock rows the command drops. ``rescued_keys``
    are locked rows intent, resolution or reachability still needs; ``retained_keys``
    are candidates kept for safety (incomplete evidence, undeleted files or
    their pinned ancestors). ``removed_files`` and ``claimed_files`` split a
    removed package's deployed files into those removal may delete and those
    a retained owner still claims. ``removed_paths`` are ``apm_modules``
    package roots to delete, ``path_keys`` the lock rows each one releases,
    ``retained_paths`` installed roots kept as required package content, and
    ``stale_keys`` lock rows without removable package content.
    ``superseded_roots`` pairs each released root with the retained package
    subtrees it still hosts. ``rescued_roots`` are an uninstall's own removal
    roots that a surviving package still reaches: only their declaration goes,
    their lock row stays with its content. ``rescued_parents``,
    ``rescued_depths`` and ``rescued_dev`` give the parent, depth and dev
    status every row the walk reaches from those roots takes, as a fresh
    install records them (#4833). ``unverified_keys`` names each lock row
    prune kept only because the graph could not prove it unreachable, with
    the reason (#4989); ``anchored_keys`` names each removed row prune keeps
    because a survivor still resolves through it under another identity.
    """

    operation: OrphanOperation
    candidate_keys: frozenset[str] = frozenset()
    removed_keys: frozenset[str] = frozenset()
    rescued_keys: frozenset[str] = frozenset()
    rescued_roots: frozenset[str] = frozenset()
    retained_keys: frozenset[str] = frozenset()
    removed_files: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    claimed_files: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    removed_paths: tuple[str, ...] = ()
    path_keys: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))
    retained_paths: tuple[str, ...] = ()
    stale_keys: tuple[str, ...] = ()
    expected_paths: frozenset[str] = frozenset()
    unverified_keys: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    anchored_keys: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    superseded_roots: tuple[tuple[str, frozenset[str]], ...] = ()
    repairs: Mapping[str, tuple[str, str | None, bool]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    rescued_parents: Mapping[str, tuple[str, str | None]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    rescued_depths: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    rescued_dev: Mapping[str, bool] = field(default_factory=lambda: MappingProxyType({}))
    reachability: ReachabilityResult | None = None

    @staticmethod
    def failed_removal_chains(
        lockfile: LockFile, pruned_keys: Collection[str], failed_keys: Collection[str]
    ) -> frozenset[str]:
        """The lock rows prune keeps after removals failed: each failed row and its
        pruned parent chain, so a retry can read the graph and finish (#5238)."""
        return _pin_retained_ancestors(
            lockfile, frozenset({*pruned_keys, *failed_keys}), frozenset(failed_keys)
        )

    @property
    def reachability_complete(self) -> bool:
        """False when a reachability walk could not verify every manifest."""
        return self.reachability is None or self.reachability.complete

    def superseded_entries(self, entries: Iterable[str]) -> list[str]:
        """Select case-folded entries of a superseded root no retained package needs."""
        retained = {path.replace("\\", "/").casefold() for path in self.expected_paths}
        return _find_orphaned_packages(entries, retained)


_REQUIRED_INPUTS: dict[OrphanOperation, tuple[str, ...]] = {
    OrphanOperation.INSTALL: ("intended", "deployment_claims"),
    OrphanOperation.TARGETED_INSTALL: (),
    OrphanOperation.PRUNE: ("installed_paths", "intended", "reachability"),
    OrphanOperation.UNINSTALL: ("reachability",),
    OrphanOperation.AUDIT: ("intended",),
    OrphanOperation.INSPECT: ("installed_paths", "intended", "reachability"),
}


def decide_orphan_removal(
    *,
    operation: OrphanOperation,
    roots: Sequence[object],
    candidate_universe: LockFile | None,
    installed_paths: InstalledPaths | None,
    resolved_keys: Collection[str] | None,
    intended: Collection[str | DependencyReference] | None,
    materialized: Sequence[DependencyReference] | None,
    reachability: ReachabilityProbe | None,
    deployment_claims: DeploymentClaims | None,
) -> OrphanRemovalDecision:
    """Return *operation*'s removal decision from explicit evidence.

    Args:
        operation: The command asking.
        roots: Removal roots whose locked descendants are candidates
            (uninstall targets, targeted-install selections); ``()`` for
            whole-graph operations.
        candidate_universe: The lockfile whose rows may be removed.
        installed_paths: The ``apm_modules`` root, plus scanned package paths
            for prune and inspection.
        resolved_keys: Keys the complete resolution retains (targeted
            install); ``None`` when resolution was incomplete, which removes
            nothing.
        intended: Manifest intent: declared references or resolved keys. A
            targeted install settles its resolve-time decision by passing
            every previous key except the dropped descendants.
        materialized: References this run materializes in ``apm_modules``,
            whose copies removal must keep.
        reachability: Forward-walk inputs; the owner fails closed on an
            incomplete walk.
        deployment_claims: Deployed-file claims of retained owners and the
            files the cleanup owner could not delete.
    """
    supplied = {
        "installed_paths": installed_paths,
        "intended": intended,
        "reachability": reachability,
        "deployment_claims": deployment_claims,
    }
    missing = [name for name in _REQUIRED_INPUTS[operation] if supplied[name] is None]
    if missing:
        raise ValueError(f"{operation.value} orphan decision requires {', '.join(missing)}")
    lockfile = candidate_universe
    if operation in (OrphanOperation.PRUNE, OrphanOperation.INSPECT):
        return _decide_installed_paths(operation, lockfile, installed_paths, intended, reachability)
    if lockfile is None:
        return OrphanRemovalDecision(operation=operation)
    if operation is OrphanOperation.UNINSTALL:
        return _decide_uninstall(lockfile, roots, reachability)
    if operation is OrphanOperation.AUDIT:
        candidates = _intent_candidates(lockfile, intended)
        return OrphanRemovalDecision(
            operation=operation,
            candidate_keys=candidates,
            removed_keys=frozenset(
                key for key in candidates if lockfile.dependencies[key].resolved_by is None
            ),
        )
    if operation is OrphanOperation.INSTALL:
        candidates = _intent_candidates(lockfile, intended)
        rescued = frozenset(lockfile.dependencies) - candidates - {_SELF_KEY}
    elif intended is not None:
        # Settling a targeted install: intent already excludes exactly the
        # descendants the resolve-time decision dropped.
        candidates, rescued = _intent_candidates(lockfile, intended), frozenset()
    else:
        candidates, rescued = _targeted_candidates(lockfile, roots, resolved_keys)
    return _settle_install(
        operation,
        lockfile,
        candidates,
        rescued,
        installed_paths,
        materialized,
        deployment_claims,
    )


def _intent_keys(
    lockfile: LockFile | None, intended: Collection[str | DependencyReference]
) -> set[str]:
    """Map manifest intent to lock keys, including a declaration's locked row."""
    keys: set[str] = set()
    marketplace_index = None
    for entry in intended:
        if isinstance(entry, str):
            keys.add(entry)
            continue
        key = entry.get_unique_key()
        keys.add(key)
        if lockfile is not None and (entry.is_marketplace or key.startswith("_marketplace/")):
            if marketplace_index is None:
                marketplace_index = lockfile.marketplace_index()
            locked = lockfile.get_dependency(key, marketplace_index=marketplace_index)
            if locked is not None:
                keys.add(locked.get_unique_key())
    return keys


def _intent_candidates(
    lockfile: LockFile, intended: Collection[str | DependencyReference]
) -> frozenset[str]:
    """Locked packages the manifest intent no longer names."""
    intent = _intent_keys(lockfile, intended)
    return frozenset(key for key in lockfile.dependencies if key != _SELF_KEY and key not in intent)


def _targeted_candidates(
    lockfile: LockFile, roots: Sequence[object], resolved_keys: Collection[str] | None
) -> tuple[frozenset[str], frozenset[str]]:
    """Split the selected roots' locked descendants into ``(dropped, rescued)``.

    Unrelated lock entries are never candidates. Nothing is dropped when
    resolution was incomplete, when a candidate carries replacement evidence
    (whose recorded parent only a complete install may rewrite), or when the
    graph omits a retained entry that may still need a candidate.
    """
    if resolved_keys is None:
        return frozenset(), frozenset()
    candidates, _ = _collect_orphan_candidates(lockfile, list(roots))
    candidates.discard(_SELF_KEY)
    if any(
        (entry := lockfile.dependencies.get(key)) is not None and entry.replacement is not None
        for key in candidates
    ):
        return frozenset(), frozenset()
    complete = frozenset(resolved_keys)
    unproven = set(lockfile.dependencies) - candidates - complete - {_SELF_KEY}
    if unproven:
        return frozenset(), frozenset(candidates & complete)
    return frozenset(candidates - complete), frozenset(candidates & complete)


def _settle_install(
    operation: OrphanOperation,
    lockfile: LockFile,
    candidates: frozenset[str],
    rescued: frozenset[str],
    installed_paths: InstalledPaths | None,
    materialized: Sequence[DependencyReference] | None,
    claims: DeploymentClaims | None,
) -> OrphanRemovalDecision:
    """Apply deployed-file claims, retry pins and package-copy selection."""
    retained_evidence = frozenset(claims.retained) if claims is not None else frozenset()
    if operation is OrphanOperation.TARGETED_INSTALL:
        retained = _pin_retained_ancestors(lockfile, candidates, candidates & retained_evidence)
    else:
        retained = candidates & retained_evidence
    released = candidates - retained
    removed_files: dict[str, tuple[str, ...]] = {}
    claimed_files: dict[str, tuple[str, ...]] = {}
    if claims is not None:
        live = claims.live_files
        if live is None:
            live = frozenset(
                path
                for key, dependency in lockfile.dependencies.items()
                if key not in candidates
                for path in dependency.deployed_files
            )
        for key in sorted(candidates):
            deployed = lockfile.dependencies[key].deployed_files
            removed_files[key] = tuple(path for path in deployed if path not in live)
            claimed_files[key] = tuple(path for path in deployed if path in live)
    decision = OrphanRemovalDecision(
        operation=operation,
        candidate_keys=candidates,
        removed_keys=released,
        rescued_keys=rescued,
        retained_keys=retained,
        removed_files=MappingProxyType(removed_files),
        claimed_files=MappingProxyType(claimed_files),
    )
    paths = (
        _released_and_retained_paths(lockfile, released, installed_paths, materialized)
        if released
        else None
    )
    if paths is None:
        return decision
    released_paths, expected = paths
    removed_paths: tuple[str, ...] = ()
    if operation is OrphanOperation.TARGETED_INSTALL:
        removed_paths = tuple(
            _find_orphaned_packages(
                (path for path in sorted(released_paths) if not Path(path).is_absolute()),
                expected,
            )
        )
    return replace(
        decision,
        removed_paths=removed_paths,
        expected_paths=frozenset(expected),
        superseded_roots=_superseded_roots(released_paths, expected),
    )


def _released_and_retained_paths(
    lockfile: LockFile,
    released: frozenset[str],
    installed_paths: InstalledPaths | None,
    materialized: Sequence[DependencyReference] | None,
) -> tuple[set[str], set[str]] | None:
    """Return ``(released, retained)`` install paths relative to ``apm_modules``.

    Retained paths cover materialized references and surviving lock rows.
    ``None`` means there is no ``apm_modules`` copy to act on or some path
    could not be computed, so no removal is provable.
    """
    if installed_paths is None or materialized is None:
        return None
    apm_modules_dir = installed_paths.root
    if not isinstance(apm_modules_dir, Path) or not apm_modules_dir.is_dir():
        return None
    dependencies = lockfile.dependencies
    try:
        retained_refs = [*materialized]
        retained_refs.extend(
            dependency.to_dependency_ref()
            for key, dependency in dependencies.items()
            if key != _SELF_KEY and key not in released
        )
        expected = _build_expected_install_paths(retained_refs, None, apm_modules_dir)
        candidates = _build_expected_install_paths(
            [
                dependency.to_dependency_ref()
                for key, dependency in sorted(dependencies.items())
                if key in released
            ],
            None,
            apm_modules_dir,
        )
    except (ValueError, PathTraversalError):
        return None
    return candidates, expected


def _superseded_roots(
    released_paths: Iterable[str], expected: Iterable[str]
) -> tuple[tuple[str, frozenset[str]], ...]:
    """Pair each released root with the retained package subtrees it hosts (#2926).

    A released copy that hosts no retained package, or that lies inside one,
    is left for ``apm prune``.
    """
    retained = {path.replace("\\", "/").casefold() for path in expected}
    roots: list[tuple[str, frozenset[str]]] = []
    for root in sorted(released_paths):
        try:
            validate_path_segments(root, context="superseded package trim")
        except PathTraversalError:
            continue
        parts = root.casefold().split("/")
        if Path(root).is_absolute() or any(
            "/".join(parts[:depth]) in retained for depth in range(1, len(parts) + 1)
        ):
            continue
        hosted = frozenset(path for path in retained if path.startswith("/".join(parts) + "/"))
        if hosted:
            roots.append((root, hosted))
    return tuple(roots)


def _pin_retained_ancestors(
    lockfile: LockFile, dropped: frozenset[str], retained: frozenset[str]
) -> frozenset[str]:
    """Keep each retained dropped entry plus its dropped ancestors for a later retry.

    A descendant whose deployed files could not be removed stays locked under
    its recorded parent; keeping that parent chain lets the next targeted
    install of the selected package rediscover and retry it. Every dropped row
    that carries a parent identity is pinned, so rows sharing one identity (a
    repository and its virtual children) never depend on set order (#4060).
    """
    by_identity: dict[str, set[str]] = {}
    for key in dropped:
        if key in lockfile.dependencies:
            identity = lockfile_parent_identity(lockfile.dependencies[key], lockfile.replacements)
            by_identity.setdefault(identity, set()).add(key)
    pinned: set[str] = set()
    queue = [key for key in dropped if key in retained]
    while queue:
        key = queue.pop()
        if key in pinned or key not in lockfile.dependencies:
            continue
        pinned.add(key)
        for parent in lockfile.dependencies[key].parent_keys:
            queue.extend(by_identity.get(parent, ()))
    return frozenset(pinned)


def _decide_installed_paths(
    operation: OrphanOperation,
    lockfile: LockFile | None,
    installed_paths: InstalledPaths,
    intended: Collection[str | DependencyReference],
    reachability: ReachabilityProbe,
) -> OrphanRemovalDecision:
    """Select installed roots unrelated to the retained graph (prune, inspection)."""
    from ..deps.reachability import retained_lock_keys

    declared = [entry for entry in intended if not isinstance(entry, str)]
    unverified: dict[str, str] = {}
    anchors: dict[str, str] = {}
    walks: list = []
    retained = (
        retained_lock_keys(
            lockfile,
            reachability.project_root,
            reachability.apm_modules_dir,
            [*reachability.direct_refs, *reachability.dev_refs],
            dev_refs=reachability.dev_refs,
            unverified=unverified,
            anchored=anchors,
            walks=walks,
        )
        if lockfile is not None
        else frozenset()
    )
    walk = walks[0] if walks and walks[0].complete else None
    declared_keys = {ref.get_unique_key() for ref in declared}
    apm_modules_dir = installed_paths.root
    expected = _build_expected_install_paths(
        declared,
        lockfile,
        apm_modules_dir,
        retained=retained,
        preserve_installed_case=True,
    )
    installed = set(installed_paths.packages or ())
    if operation is OrphanOperation.INSPECT:
        return OrphanRemovalDecision(
            operation=operation,
            removed_paths=tuple(_find_orphaned_packages(installed, expected)),
            expected_paths=frozenset(expected),
        )
    lock_keys_by_path = (
        _lock_keys_by_install_path(lockfile, apm_modules_dir) if lockfile is not None else {}
    )
    orphaned = set(_find_orphaned_packages(installed | set(lock_keys_by_path), expected))
    expected_keys = declared_keys | retained
    removed_paths = tuple(sorted(installed & orphaned))
    stale_keys = tuple(
        sorted(
            key
            for relative_path, keys in lock_keys_by_path.items()
            if relative_path not in orphaned or not (apm_modules_dir / relative_path).exists()
            for key in keys
            if key not in expected_keys
        )
    )
    path_keys = {
        path: tuple(key for key in lock_keys_by_path.get(path, ()) if key not in expected_keys)
        for path in removed_paths
    }
    removed_keys = set(stale_keys)
    for keys in path_keys.values():
        removed_keys.update(keys)
    settled = bool(removed_keys) and not unverified and not anchors
    reparented = _reparented_survivors(lockfile, removed_keys, retained, walk, settled=settled)
    repaired = _repaired_survivors(lockfile, retained, walk, reparented) if settled else []
    direct = _direct_rows(lockfile, walk, repaired) if settled else []
    return OrphanRemovalDecision(
        operation=operation,
        candidate_keys=frozenset(removed_keys),
        removed_keys=frozenset(removed_keys),
        removed_paths=removed_paths,
        path_keys=MappingProxyType(path_keys),
        retained_paths=tuple(sorted((installed - orphaned) - expected)),
        stale_keys=stale_keys,
        expected_paths=frozenset(expected),
        unverified_keys=MappingProxyType(dict(sorted(unverified.items()))),
        anchored_keys=MappingProxyType(dict(sorted(anchors.items()))),
        rescued_parents=MappingProxyType(
            {key: walk.reachable_via[key] for key in reparented} if walk else {}
        ),
        rescued_depths=MappingProxyType(
            {key: walk.depths[key] for key in (*reparented, *repaired) if key in walk.depths}
            if walk
            else {}
        ),
        rescued_dev=MappingProxyType({key: key in walk.dev_only for key in (*repaired, *direct)}),
        reachability=walk,
    )


def _reparented_survivors(lockfile, removed_keys, retained, walk, *, settled: bool) -> list[str]:
    """Retained rows recorded under a parent this prune removes (#4989).

    They take the parent a fresh install records, from the complete walk;
    without one they keep their rows unchanged. A row whose recorded parent
    survives but no longer declares it is re-derived only when *settled*
    (nothing kept fail-closed), since its dev status is repaired with it.
    """
    if walk is None or lockfile is None or not removed_keys:
        return []
    gone = {
        lockfile_parent_identity(lockfile.dependencies[key], lockfile.replacements)
        for key in removed_keys
        if key in lockfile.dependencies
    }
    from ..deps.reachability import parent_identity_counts

    identity_counts = parent_identity_counts(lockfile)
    return sorted(
        key
        for key in retained
        if key in lockfile.dependencies
        and key in walk.reachable_via
        and (
            lockfile.dependencies[key].resolved_by in gone
            or (settled and _stale_local_parent(lockfile, walk, key, identity_counts))
        )
    )


def _stale_local_parent(lockfile, walk, key, identity_counts) -> bool:
    """A local survivor whose recorded parent no longer declares it (#5126).

    Its surviving parent dropped the edge, or its declaring parent was pruned
    while ``resolved_by`` still names a survivor. It is re-derived only when
    the walk's nearest parent can record it (see ``reparent_blocker``).
    """
    from ..deps.reachability import reparent_blocker

    row = lockfile.dependencies[key]
    if row.source != "local" or row.replacement is not None or not row.depth or row.depth < 2:
        return False
    edges = walk.declaring_edges.get(key, {})
    parents = {
        lockfile_parent_identity(lockfile.dependencies[parent], lockfile.replacements)
        for parent in edges
        if parent in lockfile.dependencies
    }
    # resolved_by names a parent identity; declaring_parent names its exact key.
    stale = row.resolved_by not in parents or (
        row.declaring_parent is not None and row.declaring_parent not in edges
    )
    return stale and reparent_blocker(lockfile, walk, key, identity_counts) is None


def _repaired_survivors(lockfile, retained, walk, reparented) -> list[str]:
    """Surviving transitive rows whose depth and dev status a fresh resolve re-derives (#5126).

    A row qualifies when the complete walk reached it under its own lock key
    and its recorded parent is the walk's nearest parent, by identity and by
    exact key, and that parent could be recorded (``reparent_blocker``); or
    when prune re-parents it to that parent. Any other row keeps its metadata.
    """
    if walk is None or lockfile is None:
        return []
    from ..deps.reachability import parent_identity_counts, reparent_blocker

    identity_counts = parent_identity_counts(lockfile)
    repaired = []
    for key in sorted(retained):
        row = lockfile.dependencies.get(key)
        if row is None or not row.depth or row.depth < 2 or walk.depths.get(key, 1) < 2:
            continue
        parent_key = walk.parent_keys.get(key, "")
        parent = lockfile.dependencies.get(parent_key)
        if key in reparented or (
            parent is not None
            and lockfile_parent_identity(parent, lockfile.replacements) == row.resolved_by
            and row.declaring_parent in (None, parent_key)
            and reparent_blocker(lockfile, walk, key, identity_counts) is None
        ):
            repaired.append(key)
    return repaired


def _direct_rows(lockfile, walk, repaired) -> list[str]:
    """Declared rows the walk reached: their dev status is re-derived too (#5126).

    A dev declaration that lost its last production path records ``is_dev``,
    as a fresh install does. Only alongside a transitive repair's gate.
    """
    if walk is None or lockfile is None:
        return []
    return sorted(
        key
        for key in walk.reachable
        if key in lockfile.dependencies
        and key not in repaired
        and (lockfile.dependencies[key].depth or 1) < 2
    )


def _lock_keys_by_install_path(
    lockfile: LockFile, apm_modules_dir: Path
) -> dict[str, tuple[str, ...]]:
    """Index canonical lock keys by their host-blind installed path."""
    grouped: dict[str, list[str]] = {}
    for dep_key, dependency in sorted(lockfile.dependencies.items()):
        if dep_key == _SELF_KEY:
            continue
        install_path = dependency.to_dependency_ref().get_install_path(apm_modules_dir)
        try:
            relative_path = install_path.relative_to(apm_modules_dir).as_posix()
        except ValueError:
            continue
        grouped.setdefault(relative_path, []).append(dep_key)
    return {path: tuple(keys) for path, keys in grouped.items()}


def _decide_uninstall(
    lockfile: LockFile, roots: Sequence[object], probe: ReachabilityProbe
) -> OrphanRemovalDecision:
    """Decide which removed roots and descendants are truly unreachable.

    Subtracts the surviving direct dependencies and every still-declared
    lockfile entry from the backward-scan candidates, then asks
    :mod:`apm_cli.deps.reachability` whether a remaining candidate is still
    forward-reachable from a surviving package's own manifest (the
    shared/diamond-dependency rescue). The removal roots themselves are
    asked too: a root a surviving package still declares is a
    ``rescued_root`` whose declaration alone goes (#4749). An incomplete walk
    keeps every descendant candidate and rescues only the roots it already
    reached; with replacement evidence it refuses instead. ``repairs`` maps each rescued
    key to the ``(parent_repo_url, local_path, dev_only)`` of the surviving
    node that reached it; only the real run may persist them.
    """
    operation = OrphanOperation.UNINSTALL
    if not probe.apm_modules_dir.exists():
        return OrphanRemovalDecision(operation=operation)
    orphans, removed_parents = _collect_orphan_candidates(lockfile, list(roots))
    root_keys = _locked_root_keys(lockfile, roots)
    remaining = {ref.get_unique_key() for ref in (*probe.direct_refs, *probe.dev_refs)}
    for dep in lockfile.get_package_dependencies():
        key = dep.get_unique_key()
        if (
            key not in orphans
            and key not in root_keys
            and lockfile_parent_identity(dep, lockfile.replacements) not in removed_parents
        ):
            remaining.add(key)
    candidate_orphans = frozenset(orphans) - remaining
    candidate_roots = root_keys - remaining
    if not candidate_orphans and not candidate_roots:
        return OrphanRemovalDecision(operation=operation)

    from ..deps.reachability import compute_forward_reachable_keys

    walk = compute_forward_reachable_keys(
        lockfile,
        probe.project_root,
        probe.apm_modules_dir,
        list(probe.direct_refs),
        candidate_orphans | candidate_roots,
        dev_refs=probe.dev_refs,
    )
    if not walk.complete:
        unreached_roots = candidate_roots - walk.reachable
        if lockfile.replacements is not None and (candidate_orphans or unreached_roots):
            unverifiable = sorted({key for key, _reason in walk.unverifiable})
            details = ", ".join(unverifiable[:3])
            if len(unverifiable) > 3:
                details += ", ..."
            raise ValueError(
                "Cannot verify replacement dependency reachability "
                f"({len(unverifiable)} manifest(s): {details}). Restore missing or "
                "invalid package manifests before uninstalling."
            )
        # A root the partial walk already reached is still needed: reaching
        # it is proof. Unreached roots go as before, and every descendant
        # candidate is kept because the walk cannot prove it unused.
        rescued_roots = candidate_roots & walk.reachable
        parents, depths, dev = _rescued_metadata(lockfile, rescued_roots, walk, complete=False)
        return OrphanRemovalDecision(
            operation=operation,
            candidate_keys=candidate_orphans,
            retained_keys=candidate_orphans,
            rescued_roots=rescued_roots,
            repairs=MappingProxyType(_rescue_repairs(walk, rescued_roots)),
            rescued_parents=MappingProxyType(parents),
            rescued_depths=MappingProxyType(depths),
            rescued_dev=MappingProxyType(dev),
            reachability=walk if candidate_orphans else None,
        )
    rescued = candidate_orphans & walk.reachable
    rescued_roots = candidate_roots & walk.reachable
    parents, depths, dev = _rescued_metadata(lockfile, rescued_roots, walk, complete=True)
    return OrphanRemovalDecision(
        operation=operation,
        candidate_keys=candidate_orphans,
        removed_keys=candidate_orphans - walk.reachable,
        rescued_keys=rescued,
        rescued_roots=rescued_roots,
        repairs=MappingProxyType(_rescue_repairs(walk, rescued | rescued_roots)),
        rescued_parents=MappingProxyType(parents),
        rescued_depths=MappingProxyType(depths),
        rescued_dev=MappingProxyType(dev),
        reachability=walk,
    )


def _rescue_repairs(walk, keys: frozenset[str]) -> dict:
    """Map each rescued key to the surviving parent that reached it."""
    return {
        key: (*walk.reachable_via[key], key in walk.dev_only)
        for key in keys
        if key in walk.reachable_via
    }


def _rescued_graph(lockfile: LockFile, rescued_roots: frozenset[str], walk) -> list[str]:
    """Lock rows the walk reaches from the rescued roots, roots first (#4833).

    Follows every declaring edge the walk saw, not the recorded
    ``resolved_by`` chain, so a descendant another survivor also declares,
    or one recorded under a different parent, is included.
    """
    order = [key for key in sorted(rescued_roots) if key in lockfile.dependencies]
    seen = set(order)
    for key in order:
        for child in sorted(walk.graph_children.get(key, ())):
            if child not in seen and child in lockfile.dependencies:
                seen.add(child)
                order.append(child)
    return order


def _rescued_metadata(
    lockfile: LockFile, rescued_roots: frozenset[str], walk, *, complete: bool
) -> tuple[dict, dict, dict]:
    """Parent, depth and dev status install would record for the rescued graph.

    Returns ``(parents, depths, dev)``: ``parents`` maps a key to the
    ``(parent_repo_url, local_path)`` of its nearest declaring parent. A
    partial walk repairs only the rescued roots it reached (its depths may
    miss a shorter path) and proves production paths, never their absence.
    """
    affected = _rescued_graph(lockfile, rescued_roots, walk) if complete else sorted(rescued_roots)
    parents = {key: walk.reachable_via[key] for key in affected if key in walk.reachable_via}
    depths = {key: walk.depths[key] for key in affected if key in walk.depths}
    dev = {key: key in walk.dev_only for key in affected if key in walk.reachable}
    if not complete:
        dev = {key: is_dev for key, is_dev in dev.items() if not is_dev}
    return parents, depths, dev


def _locked_root_keys(lockfile: LockFile, roots: Sequence[object]) -> frozenset[str]:
    """Lock keys of the removal roots that hold a package row."""
    keys: set[str] = set()
    for root in roots:
        try:
            key = parse_dependency_entry(root).get_unique_key()
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        if key in lockfile.dependencies and key != _SELF_KEY:
            keys.add(key)
    return frozenset(keys)


def _build_expected_install_paths(
    declared_deps,
    lockfile,
    apm_modules_dir: Path,
    *,
    retained: Collection[str] = frozenset(),
    preserve_installed_case: bool = False,
) -> set:
    """Build expected package paths under *apm_modules_dir*.

    Combines declared references with the lock entries in *retained* (the
    keys ``deps.reachability.retained_lock_keys`` keeps), using
    ``get_install_path()`` for consistency with how packages are actually
    installed. Prune opts into recognizing existing case-equivalent paths
    without renaming them.
    """
    from ..deps.reachability import locked_marketplace_form

    reader = CachedMaterializationPathReader()

    def expected_path(dependency):
        if preserve_installed_case:
            return installed_package_path(dependency, apm_modules_dir, reader=reader)
        return dependency.get_install_path(apm_modules_dir)

    expected = set()
    marketplace_index = lockfile.marketplace_index() if lockfile else None
    for dep in declared_deps:
        if dep.is_marketplace and lockfile:
            dep = locked_marketplace_form(dep, lockfile, marketplace_index)
        if dep.is_marketplace:
            # An object-form declaration with no lock row was never materialized (#3960).
            continue
        install_path = expected_path(dep)
        try:
            relative_path = install_path.relative_to(apm_modules_dir)
            expected.add(relative_path.as_posix())
        except ValueError:
            expected.add(str(install_path))

    if lockfile:
        for dep in lockfile.get_package_dependencies():
            if dep.get_unique_key() in retained:
                install_path = expected_path(dep.to_dependency_ref())
                try:
                    relative_path = install_path.relative_to(apm_modules_dir)
                    expected.add(relative_path.as_posix())
                except ValueError:
                    pass
    return expected


def _find_orphaned_packages(installed: Iterable[str], expected: set[str]) -> list[str]:
    """Select roots unrelated to the retained dependency graph.

    Keep both bundled descendants and whole roots containing needed children.
    Ancestor prefixes protect only those containing roots, not their siblings.
    Unlike dependency-list classification, deletion must retain recognized
    ancestors at every depth, including aliases and hidden subdirectories.
    """
    roots = set()
    retained = set(expected)
    for path in expected:
        try:
            validate_path_segments(path, context="orphan selection")
        except PathTraversalError:
            # Invalid tokens protect only an exact match, never a wider subtree.
            continue
        normalized = path.replace("\\", "/")
        roots.add(normalized)
        parts = normalized.split("/")
        retained.update("/".join(parts[:depth]) for depth in range(1, len(parts) + 1))

    orphaned = []
    for path in installed:
        normalized = path.replace("\\", "/")
        if normalized in retained:
            continue
        parts = normalized.split("/")
        if any("/".join(parts[:depth]) in roots for depth in range(1, len(parts))):
            continue
        orphaned.append(path)
    return sorted(orphaned)


def _build_children_index(lockfile):
    """Index every recorded original parent in one pass over the locked edges."""
    children = {}
    for dep in lockfile.get_package_dependencies():
        for parent in dep.parent_keys:
            if parent is not None:
                children.setdefault(parent, []).append(dep)
    return children


def _collect_orphan_candidates(
    lockfile: LockFile, packages_to_remove: list[object]
) -> tuple[set[str], set[str]]:
    """Run the backward edge scan from the removal roots."""
    removed_parents: set[str] = set()
    for package in packages_to_remove:
        try:
            reference = parse_dependency_entry(package)
            removed_parents.add(lockfile_parent_identity(reference, lockfile.replacements))
        except (ValueError, TypeError, AttributeError, KeyError):
            removed_parents.add(str(package))
    children = _build_children_index(lockfile)
    candidates: set[str] = set()
    queue = list(removed_parents)
    while queue:
        for dependency in children.get(queue.pop(), ()):
            key = dependency.get_unique_key()
            if key in candidates:
                continue
            candidates.add(key)
            queue.append(lockfile_parent_identity(dependency, lockfile.replacements))
    return candidates, removed_parents
