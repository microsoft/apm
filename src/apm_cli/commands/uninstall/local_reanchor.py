"""Keep a still-needed LOCAL dependency by re-anchoring it on uninstall (#4889).

A local lock key embeds where the dependency was declared: a root-declared
``./vendor/e`` and the transitive ``local:vendor/e`` are different
identities. When ``apm uninstall ./vendor/e`` drops the root declaration but
a surviving package still declares ``e``, the row must take the identity
``apm install --no-frozen`` records for it, or ``--frozen`` fails.

Resolution is the offline reachability walk
(:func:`apm_cli.deps.reachability.compute_forward_reachable_keys`): the
install resolver's own read-only expander plus
:func:`apm_cli.deps.local_identity.transitive_local_identity`, without any
download. Its nearest parent, depth and dev status already match a fresh
install (#4804, #4833). An incomplete walk refuses before any write: a
re-anchor never fetches.

The plan re-keys, in one in-memory transition: the lock row (``local_path``
as the new parent declares it, ``declaring_parent`` and
``anchored_local_path``), every reference to the old key (descendants'
``declaring_parent`` and ``resolved_by``, replacement request parents per
req-rpl-003) and the lock's deployment-ledger owners. Receipt bindings are
verified before any write and renamed after the lock commits, carrying
their hash history (:mod:`apm_cli.core.deployment_reanchor`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


class LocalReanchorRefused(ValueError):
    """A still-needed local dependency cannot be re-anchored; nothing was written."""


@dataclass(frozen=True)
class LocalReanchorPlan:
    """Old lock key -> new key, plus the identity fields every re-keyed row takes."""

    renames: dict[str, str] = field(default_factory=dict)
    # (local_path, declaring_parent, anchored_local_path, repo_url) per old key.
    identities: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)
    # Descendant local rows: (declaring parent key, local_path) of the first edge.
    declaring_parents: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    # Nearest parent identity of every re-parented row, ranked by the new keys.
    resolved_by: dict[str, str | None] = field(default_factory=dict)
    # apm.yml and apm.lock.yaml as the command found them, for rollback.
    snapshot: tuple[tuple[Path, str], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.renames or self.declaring_parents)


def _nearest_parents(walk, key: str) -> list[str]:
    """The node's shallowest declaring parents, in the order the walk saw their edges."""
    depth = walk.depths.get(key)
    edges = walk.declaring_edges.get(key, {})
    return [
        parent for parent in edges if depth is not None and walk.depths.get(parent) == depth - 1
    ]


def _root_identities(
    lockfile, walk, local_roots, label, modules_dir: Path
) -> dict[str, tuple[str, str, Any, str]]:
    """``(local_path, declaring parent key, anchored dir, repo_url)`` of each root.

    Install keeps the first shallowest declaring edge on the node it creates,
    so that edge, not the nearest-parent tie rule, names the local identity.
    """
    identities: dict[str, tuple[str, str, Any, str]] = {}
    for key in local_roots:
        nearest = _nearest_parents(walk, key)
        real_dir = walk.local_dirs.get(key)
        via = walk.declaring_edges.get(key, {}).get(nearest[0]) if nearest else None
        if via is None or via[1] is None or real_dir is None:
            raise LocalReanchorRefused(
                f"{label(key)} stays, but its new declaring parent could not be determined"
            )
        declared = walk.local_edge_repos.get((key, nearest[0]))
        row = lockfile.get_dependency(key)
        if declared != row.repo_url and not _same_install_dir(row, declared, modules_dir):
            # An alias spelling (a symlink) installs as another package directory.
            raise LocalReanchorRefused(
                f"{label(key)} stays, but the package that still needs it declares it as "
                f"{declared or 'another package'}, which installs elsewhere"
            )
        identities[key] = (via[1], nearest[0], real_dir, declared)
    return identities


def _same_install_dir(row, declared: str | None, modules_dir: Path) -> bool:
    """Whether *declared* names *row*'s own package directory (a case-insensitive filesystem)."""
    if declared is None or declared.casefold() != row.repo_url.casefold():
        return False
    ref = row.to_dependency_ref()
    try:
        current = ref.get_install_path(modules_dir)
        spelled = replace(ref, repo_url=declared).get_install_path(modules_dir)
        return current.exists() and spelled.exists() and current.samefile(spelled)
    except (OSError, ValueError):
        return False


def plan_local_reanchor(
    lockfile, decision, project_root, rescued_roots, *, label
) -> LocalReanchorPlan:
    """Return the re-anchor plan for the still-needed local roots, or refuse.

    *rescued_roots* are the lock keys of still-needed packages whose own
    declaration goes; *label* names one for messages. Every row the walk
    re-parents also gets the parent install records, ranked by its new keys.
    """
    from ...deps.local_identity import transitive_local_identity

    local_roots = [
        key
        for key in sorted(rescued_roots)
        if (row := lockfile.get_dependency(key)) is not None and row.source == "local"
    ]
    if not local_roots:
        return LocalReanchorPlan()
    walk = getattr(decision, "reachability", None)
    if walk is None or not walk.complete:
        unverifiable = sorted({key for key, _reason in getattr(walk, "unverifiable", ())})
        detail = f" ({', '.join(unverifiable[:3])})" if unverifiable else ""
        raise LocalReanchorRefused(
            f"{label(local_roots[0])} stays, but the packages that still need it could not "
            f"all be read offline{detail}; re-anchoring it never fetches"
        )
    # The identity install records, from the one owner of local node identity.
    modules_dir = Path(project_root) / "apm_modules"
    nodes = {
        key: transitive_local_identity(
            replace(
                lockfile.get_dependency(key).to_dependency_ref(),
                local_path=local_path,
                repo_url=repo_url,
            ),
            declaring_parent=parent_key,
            anchored=real_dir,
            root=project_root,
            root_local_dirs={},
        )
        for key, (local_path, parent_key, real_dir, repo_url) in _root_identities(
            lockfile, walk, local_roots, label, modules_dir
        ).items()
    }
    renames = {key: node.get_unique_key() for key, node in nodes.items()}
    collisions = sorted(
        new for old, new in renames.items() if new != old and new in lockfile.dependencies
    )
    if collisions:
        raise LocalReanchorRefused(
            f"{label(local_roots[0])} stays, but its new lock identity {collisions[0]} "
            "already belongs to another entry"
        )
    # A re-anchored parent of another re-anchored root is named by its new key.
    identities = {
        key: (
            node.local_path,
            renames.get(node.declaring_parent, node.declaring_parent),
            node.anchored_local_path,
            node.repo_url,
        )
        for key, node in nodes.items()
    }
    declaring_parents: dict[str, tuple[str, str | None]] = {}
    resolved_by: dict[str, str | None] = {}
    for key in decision.rescued_parents:
        nearest = _nearest_parents(walk, key)
        if not nearest:
            continue
        edges = walk.declaring_edges[key]
        # The nearest-parent tie goes to the lowest key, as install ranks the new keys.
        resolved_by[key] = edges[min(nearest, key=lambda parent: renames.get(parent, parent))][0]
        row = lockfile.get_dependency(key)
        if row is not None and row.source == "local" and key not in nodes and row.declaring_parent:
            first = nearest[0]
            declaring_parents[key] = (renames.get(first, first), edges[first][1])
    return LocalReanchorPlan(renames, identities, declaring_parents, resolved_by)


def _rekey_requests(binding: Any, renames: dict[str, str], dev: dict[str, bool]) -> bool:
    """Name each replacement request's parent by its re-anchored key (req-rpl-003).

    A re-anchored parent's request also takes the parent's new dev status,
    as install records the edge.
    """
    if binding is None or not any(request.parent in renames for request in binding.requests):
        return False
    binding.requests = {
        replace(
            request,
            parent=renames.get(request.parent, request.parent),
            dev=dev.get(request.parent, request.dev),
        ): None
        for request in binding.requests
    }
    return True


def _rename_ledger_owners(lockfile, renames: dict[str, str]) -> None:
    from ...core.deployment_ledger import DeploymentLedgerCodec
    from ...core.deployment_state import DeploymentLedger

    ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    records = {}
    for key, record in ledger.records.items():
        owners = tuple(renames.get(owner, owner) for owner in record.owners)
        active = renames.get(record.active_owner, record.active_owner)
        records[key] = (
            replace(record, owners=owners, active_owner=active)
            if owners != record.owners or active != record.active_owner
            else record
        )
    lockfile.deployment_ledger = DeploymentLedger(records=records)


def apply_local_reanchor(lockfile, plan: LocalReanchorPlan, dev: dict | None = None) -> bool:
    """Apply *plan* to the in-memory lock; the caller persists it once.

    *dev* maps an old key to the dev status install now records for it.
    """
    if not plan:
        return False
    renames = {old: new for old, new in plan.renames.items() if old != new}
    for key, (local_path, parent_key, anchored, repo_url) in plan.identities.items():
        row = lockfile.dependencies[key]
        row.local_path, row.declaring_parent, row.anchored_local_path = (
            local_path,
            parent_key,
            anchored,
        )
        row.repo_url = repo_url
    for key, (parent_key, local_path) in plan.declaring_parents.items():
        row = lockfile.dependencies[key]
        row.declaring_parent = parent_key
        if local_path is not None:
            row.local_path = local_path
    for key, parent in plan.resolved_by.items():
        if key in lockfile.dependencies and parent is not None:
            lockfile.dependencies[key].resolved_by = parent
    if renames:
        _rename_ledger_owners(lockfile, renames)
        rows = list(lockfile.dependencies.items())
        lockfile.dependencies.clear()
        for key, row in rows:
            if row.declaring_parent in renames:
                row.declaring_parent = renames[row.declaring_parent]
            if row.resolved_by in renames:
                row.resolved_by = renames[row.resolved_by]
            _rekey_requests(row.replacement, renames, dev or {})
            lockfile.dependencies[renames.get(key, key)] = row
    return True


def snapshot_for_rollback(plan: LocalReanchorPlan, paths: tuple[Path, ...]) -> LocalReanchorPlan:
    """Record *paths* as they are now, so a failed receipt rename rolls them back."""
    if not plan.renames:
        return plan
    return replace(
        plan,
        snapshot=tuple(
            # Decoded bytes, not read_text(): a CRLF file restores byte for byte.
            (path, path.read_bytes().decode("utf-8"))
            for path in paths
            if path.is_file()
        ),
    )


def roll_back(plan: LocalReanchorPlan) -> None:
    """Restore apm.yml and apm.lock.yaml to the snapshot; receipts were never moved."""
    from ...integration.write_seam import atomic_write_text

    for path, text in plan.snapshot:
        atomic_write_text(path, text, normalize_line_endings=False)
