"""Canonical deployment ownership and reconciliation types."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector


class LocatorKind(str, Enum):
    """Storage form used to persist a deployed resource locator."""

    PROJECT_RELATIVE = "project-relative"
    TARGET_RELATIVE = "target-relative"
    URI = "uri"


class MaterializationStatus(str, Enum):
    """Outcome reported by a deployment adapter."""

    WRITTEN = "written"
    ADOPTED = "adopted"
    UNCHANGED = "unchanged"
    REMOVED = "removed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class DeploymentLocator:
    """Stable identity for one deployed file or native runtime entry."""

    kind: LocatorKind
    target: str
    value: str
    runtime: str | None
    scope: str

    @property
    def key(self) -> str:
        """Return the stable composite ledger key."""
        return f"{self.target}|{self.runtime or ''}|{self.scope}|{self.value}"


@dataclass(frozen=True)
class NativePayloadValidation:
    """Validation result for an adapter's native payload."""

    valid: bool
    contract: str
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaterializationResult:
    """One adapter materialization and its ownership proof."""

    locator: DeploymentLocator
    owners: frozenset[str]
    status: MaterializationStatus
    content_hash: str | None
    validation: NativePayloadValidation


@dataclass(frozen=True)
class DeploymentRecord:
    """Persisted ownership metadata for one locator."""

    locator: DeploymentLocator
    owners: tuple[str, ...]
    active_owner: str
    content_hash: str | None

    def __post_init__(self) -> None:
        """Reject records whose active owner has no ownership claim."""
        if self.active_owner not in self.owners:
            raise ValueError("active_owner must be present in owners")


@dataclass(frozen=True)
class DeploymentLedger:
    """Immutable deployment record collection keyed by locator key."""

    records: Mapping[str, DeploymentRecord]


@dataclass(frozen=True)
class DeploymentIntent:
    """Target and package universe governing one reconciliation."""

    active_targets: frozenset[str]
    declared_targets: frozenset[str] | None
    desired_owners: frozenset[str] | None
    authoritative_targets: bool
    dry_run: bool = False


@dataclass(frozen=True)
class DeploymentReconcileResult:
    """Atomic next-ledger decision and cleanup work."""

    ledger: DeploymentLedger
    removed: tuple[DeploymentLocator, ...]
    retained: tuple[DeploymentLocator, ...]
    owner_handoffs: tuple[tuple[str, str, str], ...]
    failed: tuple[DeploymentLocator, ...]
    changed: bool


@dataclass(frozen=True)
class PackageDeploymentClaims:
    """Legacy per-package claims after deterministic ownership handoff."""

    current_files: tuple[str, ...]
    prior_files: tuple[str, ...]
    prior_hashes: dict[str, str]


_SUCCESS_STATUSES = frozenset(
    {
        MaterializationStatus.WRITTEN,
        MaterializationStatus.ADOPTED,
        MaterializationStatus.UNCHANGED,
    }
)


class DeploymentReconciler:
    """Compute the next canonical ledger from adapter materializations."""

    def __init__(
        self,
        project_root: Path,
        target_profiles: Mapping[str, TargetProfile],
        *,
        diagnostics: DiagnosticCollector,
    ) -> None:
        self.project_root = project_root
        self.target_profiles = target_profiles
        self.diagnostics = diagnostics

    @staticmethod
    def reconcile_package_claims(
        *,
        package_keys: Iterable[str],
        current_claims: Mapping[str, Iterable[str]],
        prior_files: Mapping[str, Iterable[str]],
        prior_hashes: Mapping[str, Mapping[str, str]],
    ) -> dict[str, PackageDeploymentClaims]:
        """Resolve legacy last-writer claims and prior carry-forward eligibility."""
        normalized_current = {owner: tuple(paths) for owner, paths in current_claims.items()}
        last_owner: dict[str, str] = {}
        for owner, paths in normalized_current.items():
            for path in paths:
                last_owner[path] = owner

        claims: dict[str, PackageDeploymentClaims] = {}
        for owner in package_keys:
            current = tuple(
                path for path in normalized_current.get(owner, ()) if last_owner[path] == owner
            )
            eligible_prior = tuple(
                path for path in prior_files.get(owner, ()) if last_owner.get(path, owner) == owner
            )
            eligible_prior_set = set(eligible_prior)
            claims[owner] = PackageDeploymentClaims(
                current_files=current,
                prior_files=eligible_prior,
                prior_hashes={
                    path: content_hash
                    for path, content_hash in prior_hashes.get(owner, {}).items()
                    if path in eligible_prior_set
                },
            )
        return claims

    def reconcile(
        self,
        previous: DeploymentLedger,
        materializations: Iterable[MaterializationResult],
        intent: DeploymentIntent,
    ) -> DeploymentReconcileResult:
        """Return one deterministic next-ledger decision.

        Adapters own native encoding, validation, and physical cleanup. This
        method owns ordering, target contraction, ownership transfer, and the
        single ledger transition.
        """
        results = tuple(materializations)
        invalid = tuple(result for result in results if not result.validation.valid)
        if invalid:
            for result in invalid:
                detail = "; ".join(result.validation.errors)
                self.diagnostics.error(
                    (
                        f"Rejected invalid {result.validation.contract} payload "
                        f"for {result.locator.key}"
                    ),
                    detail=detail,
                )
            return self._unchanged(previous, tuple(result.locator for result in invalid))
        if intent.dry_run:
            return self._unchanged(previous, ())

        successful: dict[str, list[MaterializationResult]] = {}
        failed_by_key: dict[str, DeploymentLocator] = {}
        removed_by_key: dict[str, DeploymentLocator] = {}
        for result in results:
            key = result.locator.key
            if result.status in _SUCCESS_STATUSES:
                successful.setdefault(key, []).append(result)
            elif result.status == MaterializationStatus.REMOVED:
                removed_by_key[key] = result.locator
            elif result.status in {
                MaterializationStatus.FAILED,
                MaterializationStatus.SKIPPED,
            }:
                failed_by_key[key] = result.locator

        next_records: dict[str, DeploymentRecord] = {}
        removed: list[DeploymentLocator] = []
        retained: list[DeploymentLocator] = []
        handoffs: list[tuple[str, str, str]] = []

        for key, prior in previous.records.items():
            proofs = successful.get(key)
            if proofs:
                next_record = self._record_from_proofs(proofs, intent)
                if next_record is None:
                    failed_by_key[key] = prior.locator
                    next_records[key] = prior
                    retained.append(prior.locator)
                    continue
                if (
                    prior.active_owner != next_record.active_owner
                    and prior.active_owner not in next_record.owners
                ):
                    handoffs.append((key, prior.active_owner, next_record.active_owner))
                next_records[key] = next_record
                continue

            if key in failed_by_key:
                next_records[key] = prior
                retained.append(prior.locator)
                continue
            if key in removed_by_key or self._is_stale(prior, intent):
                removed.append(prior.locator)
                continue

            preserved = self._filter_absent_owners(prior, intent)
            if preserved is None:
                removed.append(prior.locator)
            else:
                next_records[key] = preserved
                retained.append(prior.locator)

        for key, proofs in successful.items():
            if key in previous.records:
                continue
            record = self._record_from_proofs(proofs, intent)
            if record is None:
                failed_by_key[key] = proofs[-1].locator
                continue
            next_records[key] = record

        next_ledger = DeploymentLedger(records=next_records)
        return DeploymentReconcileResult(
            ledger=next_ledger,
            removed=tuple(removed),
            retained=tuple(retained),
            owner_handoffs=tuple(handoffs),
            failed=tuple(failed_by_key.values()),
            changed=dict(previous.records) != next_records,
        )

    def _record_from_proofs(
        self,
        proofs: list[MaterializationResult],
        intent: DeploymentIntent,
    ) -> DeploymentRecord | None:
        owner_order: list[str] = []
        for proof in proofs:
            for owner in sorted(proof.owners):
                if intent.desired_owners is not None and owner not in intent.desired_owners:
                    continue
                if owner in owner_order:
                    owner_order.remove(owner)
                owner_order.append(owner)
        if not owner_order:
            proof = proofs[-1]
            self.diagnostics.error(f"Materialization {proof.locator.key} has no ownership metadata")
            return None
        active_proof = next(
            proof
            for proof in reversed(proofs)
            if any(owner in owner_order for owner in proof.owners)
        )
        active_candidates = sorted(set(active_proof.owners).intersection(owner_order))
        active_owner = active_candidates[-1]
        return DeploymentRecord(
            locator=proofs[-1].locator,
            owners=tuple(owner_order),
            active_owner=active_owner,
            content_hash=active_proof.content_hash,
        )

    def _filter_absent_owners(
        self,
        prior: DeploymentRecord,
        intent: DeploymentIntent,
    ) -> DeploymentRecord | None:
        if intent.desired_owners is None:
            return prior
        survivors = tuple(owner for owner in prior.owners if owner in intent.desired_owners)
        if not survivors:
            return None
        if survivors == prior.owners:
            return prior
        active_owner = (
            prior.active_owner if prior.active_owner in survivors else sorted(survivors)[-1]
        )
        return DeploymentRecord(
            locator=prior.locator,
            owners=survivors,
            active_owner=active_owner,
            content_hash=prior.content_hash,
        )

    def _is_stale(self, record: DeploymentRecord, intent: DeploymentIntent) -> bool:
        target = record.locator.target
        if intent.authoritative_targets and target in intent.active_targets:
            return True
        return (
            intent.declared_targets is not None
            and target in self.target_profiles
            and target not in intent.declared_targets
            and target not in intent.active_targets
        )

    @staticmethod
    def _unchanged(
        previous: DeploymentLedger,
        failed: tuple[DeploymentLocator, ...],
    ) -> DeploymentReconcileResult:
        return DeploymentReconcileResult(
            ledger=previous,
            removed=(),
            retained=tuple(record.locator for record in previous.records.values()),
            owner_handoffs=(),
            failed=failed,
            changed=False,
        )
