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
    identities: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    declaring_parents: dict[str, str] = field(default_factory=dict)
    # apm.yml and apm.lock.yaml as the command found them, for rollback.
    snapshot: tuple[tuple[Path, str], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.renames or self.declaring_parents)


def plan_local_reanchor(
    lockfile, decision, project_root, rescued_roots, *, label
) -> LocalReanchorPlan:
    """Return the re-anchor plan for the still-needed local roots, or refuse.

    *rescued_roots* are the lock keys of still-needed packages whose own
    declaration goes; *label* names one for messages. Every local row the
    walk re-parents also gets the ``declaring_parent`` install records.
    """
    from ...deps.local_identity import portable_anchor_identity

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
    renames: dict[str, str] = {}
    identities: dict[str, tuple[str, str, str]] = {}
    for key in local_roots:
        parent_key = walk.parent_keys.get(key)
        via = walk.reachable_via.get(key)
        real_dir = walk.local_dirs.get(key)
        if parent_key is None or via is None or via[1] is None or real_dir is None:
            raise LocalReanchorRefused(
                f"{label(key)} stays, but its new declaring parent could not be determined"
            )
        anchored = portable_anchor_identity(real_dir, project_root)
        identities[key] = (via[1], parent_key, anchored)
    # A re-anchored parent of another re-anchored root is named by its new key.
    for key, (local_path, parent_key, anchored) in identities.items():
        row = lockfile.get_dependency(key)
        ref = replace(
            row.to_dependency_ref(),
            local_path=local_path,
            declaring_parent=parent_key,
            anchored_local_path=anchored,
        )
        renames[key] = ref.get_unique_key()
    identities = {
        key: (local_path, renames.get(parent_key, parent_key), anchored)
        for key, (local_path, parent_key, anchored) in identities.items()
    }
    collisions = sorted(
        new for old, new in renames.items() if new != old and new in lockfile.dependencies
    )
    if collisions:
        raise LocalReanchorRefused(
            f"{label(local_roots[0])} stays, but its new lock identity {collisions[0]} "
            "already belongs to another entry"
        )
    declaring_parents = {}
    for key in decision.rescued_parents:
        parent_key = walk.parent_keys.get(key)
        row = lockfile.get_dependency(key)
        if parent_key is None:
            continue
        if row is None or row.source != "local" or key in identities or not row.declaring_parent:
            continue
        declaring_parents[key] = renames.get(parent_key, parent_key)
    return LocalReanchorPlan(renames, identities, declaring_parents)


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
    for key, (local_path, parent_key, anchored) in plan.identities.items():
        row = lockfile.dependencies[key]
        row.local_path, row.declaring_parent, row.anchored_local_path = (
            local_path,
            parent_key,
            anchored,
        )
    for key, parent_key in plan.declaring_parents.items():
        lockfile.dependencies[key].declaring_parent = parent_key
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
            (path, path.read_text(encoding="utf-8")) for path in paths if path.is_file()
        ),
    )


def roll_back(plan: LocalReanchorPlan) -> None:
    """Restore apm.yml and apm.lock.yaml to the snapshot; receipts were never moved."""
    from ...integration.write_seam import atomic_write_text

    for path, text in plan.snapshot:
        atomic_write_text(path, text, normalize_line_endings=False)
