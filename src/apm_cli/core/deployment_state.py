"""Canonical deployment ownership and reconciliation types."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core.receipt_history import is_canonical_bundle_hash

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


def require_local_bundle_hash(path: str, content_hash: str | None) -> None:
    """Reject imperative provenance without one canonical SHA-256 digest."""
    if not is_canonical_bundle_hash(content_hash):
        raise ValueError(
            f"Local bundle deployment {path!r} requires a canonical sha256:<hex> content hash"
        )


def validate_deployment_rows(rows: object) -> None:
    """Reject any malformed deployment row before ledger construction."""
    if not isinstance(rows, list):
        raise ValueError("Lockfile deployments must be a list")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"Lockfile deployment row {index} must be a mapping")
        try:
            LocatorKind(str(row["kind"]))
            target = row["target"]  # architecture-authority-exempt: ledger-row
            value = row["value"]
            scope = row["scope"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Lockfile deployment row {index} has invalid locator") from exc
        if not all(isinstance(item, str) and item for item in (target, value, scope)):
            raise ValueError(
                f"Lockfile deployment row {index} locator fields must be non-empty strings"
            )
        owners = row.get("owners")
        active_owner = row.get("active_owner")
        if (
            not isinstance(owners, list)
            or not owners
            or not all(isinstance(owner, str) and owner for owner in owners)
            or not isinstance(active_owner, str)
            or active_owner not in owners
        ):
            raise ValueError(f"Lockfile deployment row {index} has invalid owners")
        content_hash = row.get("content_hash")
        if content_hash is not None and not isinstance(content_hash, str):
            raise ValueError(
                f"Lockfile deployment row {index} content_hash must be a string or null"
            )


@dataclass(frozen=True)
class CompletedReintegration:
    """Completed skill projection, never whole-package or other-primitive authority.

    Construct only from positive skill discovery and complete materialization.
    Skill values are the adapter's successful source-backed members, not a
    destination scan. Empty or older evidence does not establish a skill domain.
    """

    owner: str
    targets: frozenset[str]
    current_values: frozenset[str]
    skill_values: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DeploymentIntent:
    """Target and package universe governing one reconciliation."""

    active_targets: frozenset[str]
    declared_targets: frozenset[str] | None
    desired_owners: frozenset[str] | None
    authoritative_targets: bool
    dry_run: bool = False
    generic_governed_values: frozenset[str] | None = None
    unprocessed_claims: frozenset[str] = frozenset()
    """Values a declared target this run did not process still governs."""
    completed_reintegration: CompletedReintegration | None = None
    verified_absent_values: frozenset[str] = frozenset()
    retained_values: frozenset[str] = frozenset()
    """Values this run declared but skipped, kept or refused: never stale (#4230)."""


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
    def current_claimed_paths(
        current_claims: Mapping[str, Iterable[str]],
    ) -> frozenset[str]:
        """Return paths claimed after canonical last-writer handoff."""
        claims = DeploymentReconciler.reconcile_package_claims(
            package_keys=current_claims,
            current_claims=current_claims,
            prior_files={},
            prior_hashes={},
        )
        return frozenset(
            path for package_claims in claims.values() for path in package_claims.current_files
        )

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
        successful_by_value = self._successful_by_value(successful)
        failed_values = {locator.value for locator in failed_by_key.values()}
        unconfirmed_absent: set[str] = set()
        obsolete_absent = self._obsolete_absent_values(
            previous, intent, successful_by_value, failed_values, unconfirmed_absent
        )
        superseded_success_keys: set[str] = set()

        for key, prior in previous.records.items():
            proofs = successful.get(key)
            if proofs is None:
                proofs = self._superseding_generic_proofs(prior, successful_by_value)
            if self._departing_only(proofs, intent):
                # A departing owner's proof neither establishes nor erases
                # ownership: keep the row's remaining owners, else drop it, and
                # the per-key pass must not process those proofs again (#4703).
                superseded_success_keys.update(proof.locator.key for proof in proofs or ())
                preserved = self._filter_absent_owners(prior, intent)
                if preserved is None:
                    removed.append(prior.locator)
                else:
                    next_records[key] = preserved
                    retained.append(prior.locator)
                continue
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
                next_records[next_record.locator.key] = next_record
                superseded_success_keys.add(next_record.locator.key)
                continue

            if key in failed_by_key:
                next_records[key] = prior
                retained.append(prior.locator)
                continue
            if (
                key in removed_by_key
                or prior.locator.value in obsolete_absent
                or self._is_stale(prior, intent)
                or self._is_generic_stale(
                    prior,
                    intent,
                    successful_by_value,
                    failed_values,
                )
            ):
                removed.append(prior.locator)
                continue

            preserved = self._filter_absent_owners(prior, intent)
            if preserved is None:
                removed.append(prior.locator)
            else:
                next_records[key] = preserved
                retained.append(prior.locator)

        for key, proofs in successful.items():
            if key in previous.records or key in superseded_success_keys:
                continue
            record = self._record_from_proofs(proofs, intent)
            if record is None:
                failed_by_key[key] = proofs[-1].locator
                continue
            next_records[key] = record

        self._warn_unconfirmed_absent(intent, unconfirmed_absent, retained)
        next_ledger = DeploymentLedger(records=next_records)
        return DeploymentReconcileResult(
            ledger=next_ledger,
            removed=tuple(removed),
            retained=tuple(retained),
            owner_handoffs=tuple(handoffs),
            failed=tuple(failed_by_key.values()),
            changed=dict(previous.records) != next_records,
        )

    @staticmethod
    def _successful_by_value(
        successful: Mapping[str, list[MaterializationResult]],
    ) -> dict[str, list[MaterializationResult]]:
        """Group trusted current-run materializations by their persisted value."""
        by_value: dict[str, list[MaterializationResult]] = {}
        for proofs in successful.values():
            for proof in proofs:
                by_value.setdefault(proof.locator.value, []).append(proof)
        return by_value

    def _superseding_generic_proofs(
        self,
        prior: DeploymentRecord,
        successful_by_value: Mapping[str, list[MaterializationResult]],
    ) -> list[MaterializationResult] | None:
        """Return current proofs that supersede one legacy generic row by value."""
        if prior.locator.target in self.target_profiles:
            return None
        return successful_by_value.get(prior.locator.value)

    def _is_generic_stale(
        self,
        record: DeploymentRecord,
        intent: DeploymentIntent,
        successful_by_value: Mapping[str, list[MaterializationResult]],
        failed_values: set[str],
    ) -> bool:
        """Decide whether a governed generic row has no safe surviving claim."""
        if (
            record.locator.target in self.target_profiles
            or intent.generic_governed_values is None
            or record.locator.value not in intent.generic_governed_values
        ):
            return False
        value = record.locator.value
        return value not in successful_by_value and value not in failed_values

    def _obsolete_absent_values(
        self,
        previous: DeploymentLedger,
        intent: DeploymentIntent,
        successful_by_value: Mapping[str, list[MaterializationResult]],
        failed_values: set[str],
        unconfirmed: set[str],
    ) -> frozenset[str]:
        """Retire absent dependency claims only with complete owner-scoped evidence.

        Absent claims that lack that proof are added to *unconfirmed*.
        """
        from apm_cli.core.deployment_ledger import UNATTRIBUTED_TARGET

        completed = intent.completed_reintegration
        if (
            not intent.authoritative_targets
            or completed is None
            or completed.owner in {".", "legacy", "_local"}
            or not completed.targets
            or not completed.targets <= intent.active_targets
        ):
            return frozenset()
        candidates = (
            intent.verified_absent_values
            - completed.current_values
            - intent.unprocessed_claims
            - intent.retained_values
            - successful_by_value.keys()
            - failed_values
        )
        allowed_targets = completed.targets | {UNATTRIBUTED_TARGET}
        protected = {
            record.locator.value
            for record in previous.records.values()
            if record.owners != (completed.owner,)
            or record.locator.kind != LocatorKind.PROJECT_RELATIVE
            or record.locator.target not in allowed_targets
        }
        candidates -= protected
        if not candidates:
            return frozenset()
        proven_skill_values = self._proven_skill_values(completed, candidates, successful_by_value)
        unconfirmed.update(candidates - proven_skill_values)
        return frozenset(candidates & proven_skill_values)

    def _warn_unconfirmed_absent(
        self,
        intent: DeploymentIntent,
        unconfirmed: set[str],
        retained: list[DeploymentLocator],
    ) -> None:
        """Name absent claims this run actually kept; removed claims are not retained."""
        kept = unconfirmed & {locator.value for locator in retained}
        if kept and intent.completed_reintegration is not None:
            self.diagnostics.warn(
                "Historical dependency claims retained: APM cannot confirm these paths "
                "belong to a current skill. Use --verbose for retained paths and check "
                "which files the dependency currently deploys.",
                package=intent.completed_reintegration.owner,
                detail=", ".join(sorted(kept)),
            )

    def _proven_skill_values(
        self,
        completed: CompletedReintegration,
        candidates: frozenset[str] | set[str],
        successful_by_value: Mapping[str, list[MaterializationResult]],
    ) -> frozenset[str]:
        """Bind candidates to source-backed skill markers using canonical routing."""
        from apm_cli.security.primitive_discovery import primitive_surfaces

        processed = [
            self.target_profiles[name]
            for name in sorted(completed.targets)
            if name in self.target_profiles
        ]
        skill_surfaces = [
            surface
            for surface in primitive_surfaces(self.project_root, processed)
            if surface.kind == "skills"
        ]
        # Other targets' non-skill paths remain protected, even outside this run's skill scope.
        other_surfaces = [
            surface
            for surface in primitive_surfaces(
                self.project_root, list(self.target_profiles.values())
            )
            if surface.kind != "skills"
        ]
        markers = completed.skill_values & completed.current_values & successful_by_value.keys()
        roots = set()
        for marker in markers:
            marker_path = self.project_root / marker
            if (
                all(
                    proof.owners == frozenset({completed.owner})
                    and proof.locator.kind == LocatorKind.PROJECT_RELATIVE
                    and proof.locator.target in completed.targets
                    for proof in successful_by_value[marker]
                )
                and any(surface.contains(marker_path) for surface in skill_surfaces)
                and not any(surface.contains(marker_path) for surface in other_surfaces)
            ):
                roots.add(marker_path.parent)
        proven = set()
        for value in candidates:
            candidate_path = self.project_root / value
            if roots.intersection(candidate_path.parents) and not any(
                surface.contains(candidate_path) for surface in other_surfaces
            ):
                proven.add(value)
        return frozenset(proven)

    @staticmethod
    def _departing_only(
        proofs: list[MaterializationResult] | None, intent: DeploymentIntent
    ) -> bool:
        """Whether every current proof comes from an owner this run removes (#4703).

        An ownerless proof is not departing: it still reaches the ownership error.
        """
        if not proofs or intent.desired_owners is None:
            return False
        if not all(proof.owners for proof in proofs):
            return False
        return not any(owner in intent.desired_owners for proof in proofs for owner in proof.owners)

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
        """Decide whether an unrefreshed row lost its target this run.

        A row of an active target is stale unless it lies under another
        profile root of that target (``CLAUDE_CONFIG_DIR``, ``HERMES_HOME``,
        ``CODEX_HOME``).
        A row of a known target dropped from the declared set is stale too.
        """
        if not intent.authoritative_targets or record.locator.value in intent.retained_values:
            return False
        target = record.locator.target
        if target in intent.active_targets:
            from apm_cli.integration.targets import is_other_profile_path

            profile = self.target_profiles.get(target)
            return profile is None or not is_other_profile_path(profile, record.locator.value)
        return (
            intent.declared_targets is not None
            and target in self.target_profiles
            and target not in intent.declared_targets
            and target not in intent.active_targets
            and record.locator.value not in intent.unprocessed_claims
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
