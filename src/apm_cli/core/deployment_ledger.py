"""Lockfile codec for canonical deployment ownership state."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.core.deployment_state import (
    DeploymentIntent,
    DeploymentLedger,
    DeploymentLocator,
    DeploymentReconciler,
    DeploymentReconcileResult,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
)
from apm_cli.utils.paths import portable_relpath

if TYPE_CHECKING:
    from apm_cli.core.scope import InstallScope
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector


_LEGACY_TARGET_PREFIXES = {
    ".github/": "copilot",
    ".claude/": "claude",
    ".cursor/": "cursor",
    ".windsurf/": "windsurf",
    ".kiro/": "kiro",
    ".gemini/": "gemini",
    ".codex/": "codex",
    ".opencode/": "opencode",
    ".openfox/": "openfox",
}
_LEGACY_USER_TARGET_PREFIXES = {
    ".copilot/": "copilot",
}
_LOCAL_BUNDLE_OWNER = "local-bundle"
UNATTRIBUTED_TARGET = "legacy"
DEPLOYMENT_OWNER_REMEDIATION = "Run 'apm prune', then rerun 'apm audit'."
_SHA256_PREFIX = "sha256:"
_LOWER_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class DeploymentOwnerViolation:
    """One canonical deployment row with invalid owner references."""

    locator: DeploymentLocator
    owners: tuple[str, ...]
    active_owner: str
    invalid_owners: tuple[str, ...]
    invalid_active_owner: str | None


@dataclass(frozen=True)
class DeploymentCleanupSnapshot:
    """Canonical paths and hashes selected for one cleanup transition."""

    paths: frozenset[str]
    hashes: dict[str, str]


class DeploymentMaterializationSession:
    """Installation-local rendered-write evidence, subordinate to current ownership.

    Only live install and uninstall survivor boundaries create writing sessions. The checkout holds
    an ignored random identity, not overwrite evidence; the OS-user state holds
    canonical records. Neither a checked-out lock nor copied source bytes can
    create a prior-local baseline.
    """

    def __init__(self, project_root: Path, scope: InstallScope, lockfile: LockFile | None) -> None:
        self.root = project_root.resolve()
        self.scope = scope
        key = hashlib.sha256(f"{self.root}|{scope.value}".encode()).hexdigest()
        self.state_path = Path.home() / ".apm" / "deployment-receipts" / f"{key}.json"
        self.identity_path = self.root / ".apm-local" / "installation"
        self._records: dict[str, DeploymentRecord] = {}
        self._destinations: dict[str, str] = {}
        self._bindings: dict[str, list[str]] = {}
        self._written: dict[str, str] = {}
        self._dirty = False
        self._claims: dict[str, list[DeploymentRecord]] = {}
        if lockfile is not None:
            for record in DeploymentLedgerCodec.from_lockfile(lockfile).records.values():
                self._claims.setdefault(
                    DeploymentLedgerCodec.legacy_value(record.locator), []
                ).append(record)
        self._physical_keys: dict[str, str] = {}
        self._valid_owners = (
            DeploymentLedgerCodec.valid_owner_keys(lockfile) if lockfile else frozenset({"."})
        )
        self._installation: str | None = None
        self._state_present = False
        self._load()

    def _validate_paths(self) -> None:
        for base, path in (
            (Path.home(), self.state_path),
            (self.root, self.identity_path),
            (self.root, self.identity_path.parent / ".gitignore"),
        ):
            ensure_path_within(path, base)
            if has_symlink_component(base, path, raise_on_error=True):
                raise PathTraversalError(
                    f"Refusing symlinked materialization evidence at {path}. "
                    "Restore a regular local state path before installing."
                )
            if path.exists() and not path.is_file():
                raise ValueError(
                    f"Refusing non-regular materialization evidence at {path}. "
                    "Preserve this path and move it aside before retrying."
                )

    def _root_identity(self) -> list[int]:
        metadata = self.root.stat()
        return [metadata.st_dev, metadata.st_ino]

    def _load(self) -> None:
        self._validate_paths()
        self._state_present = self.state_path.exists()
        if not self.identity_path.exists():
            return
        try:
            installation = self.identity_path.read_text(encoding="ascii").strip()
            if uuid.UUID(hex=installation).hex != installation:
                raise ValueError("invalid installation identity")
            self._installation = installation
            if not self.state_path.exists():
                return
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (
                data["version"] != 1
                or data["root"] != self.root.as_posix()
                or data["scope"] != self.scope.value
                or data["installation"] != installation
                or data["root_identity"] != self._root_identity()
            ):
                raise ValueError("installation binding does not match")
            ledger = DeploymentLedgerCodec.from_rows(data["records"])
            if len(ledger.records) != len(data["records"]):
                raise ValueError("duplicate rendered-write records")
            destinations = data["destinations"]
            bindings = data["bindings"]
            if not isinstance(destinations, dict) or set(destinations) != set(ledger.records):
                raise ValueError("destination bindings do not match")
            if not isinstance(bindings, dict) or set(bindings) != set(ledger.records):
                raise ValueError("ownership bindings do not match")
            for record in ledger.records.values():
                if (
                    record.locator.kind == LocatorKind.URI
                    or record.locator.scope != self.scope.value
                    or not _is_canonical_bundle_hash(record.content_hash)
                    or not isinstance(destinations[record.locator.key], str)
                    or not isinstance(bindings[record.locator.key], list)
                    or len(bindings[record.locator.key]) != 2
                    or not all(isinstance(value, str) for value in bindings[record.locator.key])
                ):
                    raise ValueError("invalid rendered-write record")
            self._records = dict(ledger.records)
            self._destinations = destinations
            self._bindings = bindings
            self._physical_keys = {path: key for key, path in destinations.items()}
            if len(self._physical_keys) != len(destinations):
                raise ValueError("ambiguous physical write receipts")
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ValueError(
                f"Invalid local materialization evidence at {self.state_path}: {exc}. "
                "Preserve local skill edits, then remove the invalid receipt file and retry. "
                "Existing files without trustworthy evidence will not be overwritten."
            ) from exc

    def _locator(self, path: Path, targets: Sequence[TargetProfile]) -> DeploymentLocator | None:
        for target in targets:
            if not target.supports("skills"):
                continue
            try:
                ensure_path_within(path, target.skills_deploy_path(self.root))
            except PathTraversalError:
                continue
            return DeploymentLedgerCodec.locator_for_path(
                path, project_root=self.root, target=target, scope=self.scope
            )
        return None

    def baseline(self, path: Path, targets: Sequence[TargetProfile]) -> str | None:
        """Return a prior rendered hash only for a currently authorized physical member."""
        from apm_cli.install.deployed_paths import deployed_path_entry

        locator = self._locator(path, targets)
        if locator is None:
            return None
        key = self._physical_keys.get(path.resolve().as_posix())
        record = self._records.get(key)
        if record is None:
            return None
        from apm_cli.integration.targets import KNOWN_TARGETS

        recorded_target = next(
            (target for target in targets if target.name == record.locator.target), None
        )
        if recorded_target is None:
            profile = KNOWN_TARGETS.get(record.locator.target)
            recorded_target = (
                profile.for_scope(user_scope=self.scope.value == "user") if profile else None
            )
        if recorded_target is None or self._locator(path, [recorded_target]) != record.locator:
            return None
        if key in self._written:
            return record.content_hash
        value = deployed_path_entry(path, self.root, targets)
        if (
            not self._claims.get(value)
            or record.active_owner not in self._valid_owners
            or self._bindings.get(key) != [record.active_owner, self._claim_signature(value)]
        ):
            return None
        return record.content_hash

    def _claim_signature(self, value: str) -> str:
        """Bind to canonical ownership metadata, independently of the actual adapter writer."""
        return json.dumps(
            sorted(
                (record.locator.key, record.owners, record.active_owner)
                for record in self._claims.get(value, ())
            ),
            separators=(",", ":"),
        )

    def requires_target_write(
        self, path: Path, targets: Sequence[TargetProfile], owner: str
    ) -> bool:
        """Require a real guarded write when a valid same-writer receipt changes target."""
        locator = self._locator(path, targets)
        key = self._physical_keys.get(path.resolve().as_posix())
        record = self._records.get(key)
        return (
            locator is not None
            and record is not None
            and record.active_owner == owner
            and record.locator.target != locator.target
            and self.baseline(path, targets) is not None
        )

    def record(
        self,
        written: Mapping[Path, str],
        targets: Sequence[TargetProfile],
        owner: str,
    ) -> None:
        """Accumulate successful rendered writes for one atomic end-of-batch flush."""
        from apm_cli.install.deployed_paths import deployed_path_entry

        if not written:
            return
        prepared: list[tuple[DeploymentRecord, str, str]] = []
        for path, content_hash in written.items():
            locator = self._locator(path, targets)
            if locator is None or not _is_canonical_bundle_hash(content_hash):
                raise ValueError(f"Cannot record unverified skill materialization: {path}")
            prepared.append(
                (
                    DeploymentRecord(locator, (owner,), owner, content_hash),
                    path.resolve().as_posix(),
                    deployed_path_entry(path, self.root, targets),
                )
            )
        for record, physical, value in prepared:
            key = record.locator.key
            previous = self._physical_keys.get(physical)
            if previous is not None:
                self._records.pop(previous, None)
                self._destinations.pop(previous, None)
                self._bindings.pop(previous, None)
            self._records[key] = record
            self._destinations[key] = physical
            self._physical_keys[physical] = key
            self._bindings[key] = [
                record.active_owner,
                self._claim_signature(value),
            ]
            self._written[key] = value
        self._dirty = True

    def flush(self) -> None:
        """Publish successful writes once, including those preceding another package's failure."""
        if self._dirty:
            self._persist(self._records, self._destinations)
            self._dirty = False

    def bind_committed_claims(self, lockfile: LockFile | None) -> None:
        """Bind this batch's surviving writes to canonical post-install claims.

        Root-local bytes can intentionally be represented by dependency claims.
        Keep that projection separate from the actual writer; never infer a
        writer or a baseline from the committed lock.
        """
        if not self._written:
            return
        committed = type(self)(self.root, self.scope, lockfile)
        for key, value in self._written.items():
            record = committed._records.get(key)
            if record is None or record != self._records.get(key):
                continue
            committed._bindings[key] = [record.active_owner, committed._claim_signature(value)]
            committed._dirty = True
        committed.flush()

    def _persist(self, records: dict[str, DeploymentRecord], destinations: dict[str, str]) -> None:
        from apm_cli.utils.atomic_io import atomic_write_text

        self._validate_paths()
        if self._installation is None:
            self.identity_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            ignore = self.identity_path.parent / ".gitignore"
            self._validate_paths()
            if ignore.exists():
                if ignore.read_bytes() != b"*\n":
                    raise ValueError(
                        f"Refusing to replace existing installation identity ignore rules: {ignore}. "
                        "Preserve this directory's contents and move it aside before retrying."
                    )
            else:
                atomic_write_text(ignore, "*\n", new_file_mode=0o600, create_only=True)
            installation = uuid.uuid4().hex
            atomic_write_text(
                self.identity_path, installation + "\n", new_file_mode=0o600, create_only=True
            )
            self._installation = installation
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._validate_paths()
        data = {
            "version": 1,
            "root": self.root.as_posix(),
            "root_identity": self._root_identity(),
            "scope": self.scope.value,
            "installation": self._installation,
            "records": DeploymentLedgerCodec.rows(DeploymentLedger(records)),
            "destinations": destinations,
            "bindings": {key: self._bindings[key] for key in records},
        }
        atomic_write_text(
            self.state_path,
            json.dumps(data, sort_keys=True, indent=2) + "\n",
            new_file_mode=0o600,
            create_only=not self._state_present,
        )
        self._state_present = True

    def retire(self, deleted: Sequence[Path]) -> None:
        """Forget successfully removed physical outputs without granting cleanup authority."""
        removed = {path.resolve().as_posix() for path in deleted}
        keys = {
            key
            for key, value in self._destinations.items()
            if value in removed
            or any(parent.as_posix() in removed for parent in Path(value).parents)
        }
        if keys:
            records = {key: record for key, record in self._records.items() if key not in keys}
            destinations = {
                key: value for key, value in self._destinations.items() if key not in keys
            }
            self._persist(records, destinations)
            self._records = records
            self._destinations = destinations
            self._bindings = {
                key: value for key, value in self._bindings.items() if key not in keys
            }
            self._physical_keys = {path: key for key, path in destinations.items()}

    @classmethod
    def retire_removed(
        cls, project_root: Path, deleted: Sequence[Path], *, user_scope: bool
    ) -> None:
        """Route actual cleanup outcomes through the same local evidence owner."""
        if not deleted:
            return
        from apm_cli.core.scope import InstallScope

        scope = InstallScope.USER if user_scope else InstallScope.PROJECT
        cls(project_root, scope, None).retire(deleted)


def _is_canonical_bundle_hash(content_hash: str | None) -> bool:
    """Return whether *content_hash* is one canonical ``sha256:<hex>`` digest."""
    digest = (
        content_hash.removeprefix(_SHA256_PREFIX)
        if isinstance(content_hash, str) and content_hash.startswith(_SHA256_PREFIX)
        else ""
    )
    return len(digest) == 64 and all(character in _LOWER_HEX for character in digest)


def _require_local_bundle_hash(path: str, content_hash: str | None) -> None:
    """Reject imperative provenance without one canonical SHA-256 digest."""
    if not _is_canonical_bundle_hash(content_hash):
        raise ValueError(
            f"Local bundle deployment {path!r} requires a canonical sha256:<hex> content hash"
        )


class DeploymentLedgerCodec:
    """Translate canonical deployment records to and from lockfile views."""

    @staticmethod
    def deployed_file_hashes(lockfile: LockFile) -> dict[str, str]:
        """Project recorded physical-file hashes without adopting current disk content."""
        return {
            DeploymentLedgerCodec.legacy_value(record.locator): record.content_hash
            for record in DeploymentLedgerCodec.from_lockfile(lockfile).records.values()
            if record.content_hash is not None
        }

    @staticmethod
    def active_file_owners(lockfile: LockFile) -> dict[str, str]:
        """Return the recorded last writer for each compatibility path."""
        return {
            DeploymentLedgerCodec.legacy_value(record.locator): record.active_owner
            for record in DeploymentLedgerCodec.from_lockfile(lockfile).records.values()
        }

    @staticmethod
    def recorded_target_names(
        lockfile: LockFile,
        *,
        scope: str,
        supported_targets: Collection[str],
    ) -> tuple[str, ...]:
        """Project concrete target identities from recorded deployment state."""
        ledger = lockfile.deployment_ledger
        # A compatibility ledger inferred from paths is not installed target intent.
        if not lockfile._deployments_present or ledger is None:
            return ()
        return tuple(
            sorted(
                {
                    record.locator.target
                    for record in ledger.records.values()
                    if record.locator.scope == scope and record.locator.target in supported_targets
                }
            )
        )

    @staticmethod
    def legacy_deployed_file_claims(lockfile: LockFile) -> dict[str, str]:
        """Return the compatibility ``deployed_files`` view by owning package."""
        claims: dict[str, str] = {}
        for owner, dependency in lockfile.dependencies.items():
            if owner == ".":
                continue
            for path in dependency.deployed_files:
                claims.setdefault(path, owner)
        for path in lockfile.local_deployed_files:
            claims.setdefault(path, ".")
        return claims

    @staticmethod
    def legacy_deployed_file_hash_paths(lockfile: LockFile) -> frozenset[str]:
        """Return paths explicitly file-shaped by compatibility hash views."""
        paths = set(lockfile.local_deployed_file_hashes)
        for owner, dependency in lockfile.dependencies.items():
            if owner != ".":
                paths.update(dependency.deployed_file_hashes)
        return frozenset(paths)

    @staticmethod
    def cleanup_snapshot(
        lockfile: LockFile,
        dependency_keys: Collection[str],
    ) -> DeploymentCleanupSnapshot:
        """Return selected cleanup claims with canonical ledger hash precedence."""
        selected = frozenset(dependency_keys)
        paths = {
            path
            for owner, dependency in lockfile.dependencies.items()
            if owner in selected
            for path in dependency.deployed_files
        }
        hashes: dict[str, str] = {}
        for record in DeploymentLedgerCodec.from_lockfile(lockfile).records.values():
            if (
                record.locator.value in paths
                and record.content_hash
                and selected.intersection(record.owners)
            ):
                hashes.setdefault(record.locator.value, record.content_hash)
        for owner, dependency in lockfile.dependencies.items():
            if owner not in selected:
                continue
            for path, content_hash in dependency.deployed_file_hashes.items():
                hashes.setdefault(path, content_hash)
        return DeploymentCleanupSnapshot(paths=frozenset(paths), hashes=hashes)

    @staticmethod
    def valid_owner_keys(
        lockfile: LockFile,
        *,
        excluded_dependency_keys: Collection[str] = (),
    ) -> frozenset[str]:
        """Return the canonical owner universe for one lockfile transition."""
        excluded = frozenset(excluded_dependency_keys)
        dependencies = {owner for owner in lockfile.dependencies if owner not in excluded}
        return frozenset({".", _LOCAL_BUNDLE_OWNER, *dependencies})

    @staticmethod
    def owner_reference_violations(
        lockfile: LockFile,
        *,
        excluded_dependency_keys: Collection[str] = (),
        ledger: DeploymentLedger | None = None,
    ) -> tuple[DeploymentOwnerViolation, ...]:
        """Return invalid canonical owner references in locator-key order."""
        valid_owners = DeploymentLedgerCodec.valid_owner_keys(
            lockfile,
            excluded_dependency_keys=excluded_dependency_keys,
        )
        violations: list[DeploymentOwnerViolation] = []
        source_ledger = (
            ledger if ledger is not None else DeploymentLedgerCodec.from_lockfile(lockfile)
        )
        for key in sorted(source_ledger.records):
            record = source_ledger.records[key]
            invalid_owners = tuple(owner for owner in record.owners if owner not in valid_owners)
            invalid_active_owner = (
                record.active_owner if record.active_owner not in valid_owners else None
            )
            if invalid_owners or invalid_active_owner is not None:
                violations.append(
                    DeploymentOwnerViolation(
                        locator=record.locator,
                        owners=record.owners,
                        active_owner=record.active_owner,
                        invalid_owners=invalid_owners,
                        invalid_active_owner=invalid_active_owner,
                    )
                )
        return tuple(violations)

    @staticmethod
    def reconcile_owner_references(
        lockfile: LockFile,
        *,
        excluded_dependency_keys: Collection[str] = (),
        ledger: DeploymentLedger | None = None,
        project_root: Path,
        diagnostics: DiagnosticCollector,
    ) -> DeploymentReconcileResult:
        """Compute a pure owner-reference transition through the reconciler."""
        return DeploymentReconciler(
            project_root,
            {},
            diagnostics=diagnostics,
        ).reconcile(
            ledger if ledger is not None else DeploymentLedgerCodec.from_lockfile(lockfile),
            materializations=(),
            intent=DeploymentIntent(
                active_targets=frozenset(),
                declared_targets=None,
                desired_owners=DeploymentLedgerCodec.valid_owner_keys(
                    lockfile,
                    excluded_dependency_keys=excluded_dependency_keys,
                ),
                authoritative_targets=False,
            ),
        )

    @staticmethod
    def merge_dependencies(
        lockfile: LockFile,
        updated: LockFile,
        *,
        project_root: Path,
        diagnostics: DiagnosticCollector,
    ) -> None:
        """Merge processed dependencies without discarding concrete deployment locators."""
        current = DeploymentLedgerCodec.from_lockfile(updated)
        retained = DeploymentLedgerCodec.reconcile_owner_references(
            lockfile,
            excluded_dependency_keys=updated.dependencies,
            project_root=project_root,
            diagnostics=diagnostics,
        )
        # add_dependency invalidates the legacy projection. Capture both
        # canonical ledgers first, then restore their reconciled records once.
        for dependency in updated.dependencies.values():
            lockfile.add_dependency(dependency)
        DeploymentLedgerCodec.apply_to_lockfile(
            DeploymentLedger(records={**retained.ledger.records, **current.records}),
            lockfile,
        )

    @staticmethod
    def from_lockfile(lockfile: LockFile) -> DeploymentLedger:
        """Read canonical rows or synthesize them from legacy ownership views."""
        current = getattr(lockfile, "deployment_ledger", None)
        if current is not None and (
            current.records or getattr(lockfile, "_deployments_present", False)
        ):
            return current

        records: dict[str, DeploymentRecord] = {}
        for owner, dependency in lockfile.dependencies.items():
            if owner == ".":
                continue
            DeploymentLedgerCodec._add_legacy_paths(
                records,
                owner,
                dependency.deployed_files,
                dependency.deployed_file_hashes,
            )
        DeploymentLedgerCodec._add_legacy_paths(
            records,
            ".",
            lockfile.local_deployed_files,
            lockfile.local_deployed_file_hashes,
        )
        service_targets = (
            ("mcp", getattr(lockfile, "mcp_target_servers", {})),
            ("lsp", getattr(lockfile, "lsp_target_servers", {})),
        )
        for service, target_servers in service_targets:
            for runtime, servers in target_servers.items():
                for server in servers:
                    locator = DeploymentLocator(
                        kind=LocatorKind.URI,
                        target=service,
                        value=server,
                        runtime=runtime,
                        scope="project",
                    )
                    records[locator.key] = DeploymentRecord(
                        locator=locator,
                        owners=(".",),
                        active_owner=".",
                        content_hash=None,
                    )
        return DeploymentLedger(records=records)

    @staticmethod
    def apply_to_lockfile(
        ledger: DeploymentLedger,
        lockfile: LockFile,
        *,
        write_legacy_views: bool = True,
    ) -> None:
        """Apply the ledger once and optionally maintain one-cycle legacy views."""
        lockfile.deployment_ledger = ledger
        lockfile._deployments_present = True
        if not write_legacy_views:
            return

        dependency_files: dict[str, list[str]] = {
            owner: [] for owner in lockfile.dependencies if owner != "."
        }
        dependency_hashes: dict[str, dict[str, str]] = {
            owner: {} for owner in lockfile.dependencies if owner != "."
        }
        local_files: list[str] = []
        local_hashes: dict[str, str] = {}
        service_targets: dict[str, dict[str, list[str]]] = {"mcp": {}, "lsp": {}}

        for record in ledger.records.values():
            locator = record.locator
            if locator.target in service_targets and locator.runtime:
                service_targets[locator.target].setdefault(locator.runtime, []).append(
                    locator.value
                )
                continue
            path = DeploymentLedgerCodec.legacy_value(locator)
            for owner in record.owners:
                if owner == ".":
                    local_files.append(path)
                    if record.content_hash is not None:
                        local_hashes[path] = record.content_hash
                elif owner in dependency_files:
                    dependency_files[owner].append(path)
                    if record.content_hash is not None:
                        dependency_hashes[owner][path] = record.content_hash

        for owner, dependency in lockfile.dependencies.items():
            if owner == ".":
                continue
            dependency.deployed_files = sorted(dict.fromkeys(dependency_files[owner]))
            dependency.deployed_file_hashes = dict(sorted(dependency_hashes[owner].items()))
        lockfile.local_deployed_files = sorted(dict.fromkeys(local_files))
        lockfile.local_deployed_file_hashes = dict(sorted(local_hashes.items()))
        lockfile.mcp_target_servers = {
            runtime: sorted(dict.fromkeys(servers))
            for runtime, servers in sorted(service_targets["mcp"].items())
        }
        lockfile.lsp_target_servers = {
            runtime: sorted(dict.fromkeys(servers))
            for runtime, servers in sorted(service_targets["lsp"].items())
        }

    @staticmethod
    def replace_legacy_owner(
        lockfile: LockFile,
        owner: str,
        files: list[str],
        hashes: dict[str, str],
        *,
        provenance: DeploymentLedger | None = None,
        current_targets: Sequence[TargetProfile] = (),
    ) -> None:
        """Update one compatibility ownership view and invalidate its projection.

        Pass ``provenance`` when *lockfile* lacks these paths' concrete targets
        or bundle provenance, as a freshly built lockfile does; shared-root
        paths would otherwise rebuild unattributed and never prove stale.
        A concrete locator for one of *current_targets* wins, the lockfile's
        own first, so attribution stays with the install that chose it.
        Bundle paths stay unattributed so no target can sweep them.
        """
        prior_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
        prior_bundle_paths = DeploymentLedgerCodec.local_bundle_paths(lockfile)
        preferred: dict[str, DeploymentLocator] = {}
        if provenance is not None:
            owned = set(files)
            provenance_bundles = DeploymentLedgerCodec.local_bundle_values(provenance) & owned
            # Malformed provenance is dropped, as a rebuild without it would.
            prior_bundle_paths |= {
                path for path in provenance_bundles if _is_canonical_bundle_hash(hashes.get(path))
            }
            preferred = DeploymentLedgerCodec._preferred_locators(
                owned
                - DeploymentLedgerCodec.local_bundle_scope(
                    owned, prior_bundle_paths | provenance_bundles
                ),
                (prior_ledger, provenance),
                {getattr(target, "name", None) for target in current_targets},
            )
        if owner == ".":
            lockfile.local_deployed_files = list(files)
            lockfile.local_deployed_file_hashes = dict(hashes)
        else:
            dependency = lockfile.dependencies[owner]
            dependency.deployed_files = list(files)
            dependency.deployed_file_hashes = dict(hashes)
        DeploymentLedgerCodec._rebuild_from_legacy(
            lockfile,
            prior_bundle_paths,
            prior_ledger=prior_ledger,
            preferred_locators=preferred,
        )

    @staticmethod
    def _preferred_locators(
        values: Collection[str],
        sources: Sequence[DeploymentLedger],
        current: Collection[str | None],
    ) -> dict[str, DeploymentLocator]:
        """Choose one concrete project-relative locator per value in *values*.

        A current target wins, then the earlier source, then the locator key.
        """
        candidates = sorted(
            (
                (rank, record.locator)
                for rank, source in enumerate(sources)
                for record in source.records.values()
                if record.locator.value in values
                and record.locator.kind == LocatorKind.PROJECT_RELATIVE
                and record.locator.target != UNATTRIBUTED_TARGET
            ),
            key=lambda item: (item[1].target not in current, item[0], item[1].key),
        )
        preferred: dict[str, DeploymentLocator] = {}
        for _rank, locator in candidates:
            preferred.setdefault(locator.value, locator)
        return preferred

    @staticmethod
    def replace_legacy_owners(
        lockfile: LockFile,
        updates: dict[str, tuple[list[str], dict[str, str]]],
    ) -> None:
        """Apply compatibility ownership updates through one ledger rebuild."""
        if not updates:
            return
        prior_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
        prior_bundle_paths = DeploymentLedgerCodec.local_bundle_paths(lockfile)
        for owner, (files, hashes) in updates.items():
            if owner == ".":
                lockfile.local_deployed_files = list(files)
                lockfile.local_deployed_file_hashes = dict(hashes)
                continue
            dependency = lockfile.dependencies[owner]
            dependency.deployed_files = list(files)
            dependency.deployed_file_hashes = dict(hashes)
        DeploymentLedgerCodec._rebuild_from_legacy(
            lockfile,
            prior_bundle_paths,
            prior_ledger=prior_ledger,
        )

    @staticmethod
    def record_local_bundle_files(
        lockfile: LockFile,
        files: list[str],
        hashes: dict[str, str],
    ) -> None:
        """Persist imperative bundle files without making them replayable source."""
        prior_bundle_paths = DeploymentLedgerCodec.local_bundle_paths(lockfile)
        for path in files:
            _require_local_bundle_hash(path, hashes.get(path))
        merged_files = sorted(set(lockfile.local_deployed_files).union(files))
        merged_hashes = dict(lockfile.local_deployed_file_hashes)
        merged_hashes.update(hashes)
        DeploymentLedgerCodec.replace_legacy_owner(
            lockfile,
            ".",
            merged_files,
            merged_hashes,
        )

        DeploymentLedgerCodec._mark_local_bundle_paths(
            lockfile,
            prior_bundle_paths.union(files),
        )
        DeploymentLedgerCodec.apply_to_lockfile(lockfile.deployment_ledger, lockfile)

    @staticmethod
    def local_bundle_paths(lockfile: LockFile) -> frozenset[str]:
        """Return paths whose active provenance is an imperative local bundle."""
        ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
        paths: set[str] = set()
        for record in ledger.records.values():
            if record.active_owner == _LOCAL_BUNDLE_OWNER:
                path = DeploymentLedgerCodec.legacy_value(record.locator)
                _require_local_bundle_hash(path, record.content_hash)
                paths.add(path)
        return frozenset(paths)

    @staticmethod
    def authored_local_paths(lockfile: LockFile) -> frozenset[str]:
        """Return project-owned local paths an install, not a bundle, deployed."""
        return frozenset(lockfile.local_deployed_files) - DeploymentLedgerCodec.local_bundle_values(
            DeploymentLedgerCodec.from_lockfile(lockfile)
        )

    @staticmethod
    def local_bundle_values(ledger: DeploymentLedger) -> frozenset[str]:
        """Return local-bundle values without validating their hashes, for classification."""
        return frozenset(
            DeploymentLedgerCodec.legacy_value(record.locator)
            for record in ledger.records.values()
            if record.active_owner == _LOCAL_BUNDLE_OWNER
        )

    @staticmethod
    def local_bundle_scope(values: Collection[str], bundles: Collection[str]) -> frozenset[str]:
        """Return *values* that are bundle paths or directories containing one.

        Sweeping a skill directory removes everything under it, so a directory
        that holds bundle output is as protected as the bundle file itself.
        """
        covered: set[str] = set()
        for bundle in bundles:
            parts = bundle.split("/")
            covered.update("/".join(parts[:end]) for end in range(1, len(parts) + 1))
        return frozenset(value for value in values if value in covered)

    @staticmethod
    def mcp_native_hashes(lockfile: LockFile) -> dict[str, dict[str, str]]:
        """Project retained MCP URI content hashes keyed by ``runtime → server → hash``.

        Returns only rows whose ``content_hash`` is a well-formed
        ``sha256:<64-lowercase-hex>`` envelope.  Malformed or absent
        hashes are silently excluded (fail closed).
        """
        ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
        result: dict[str, dict[str, str]] = {}
        for record in ledger.records.values():
            locator = record.locator
            if (
                locator.kind is not LocatorKind.URI
                or locator.target != "mcp"
                or locator.runtime is None
                or record.content_hash is None
                or not _is_canonical_bundle_hash(record.content_hash)
            ):
                continue
            result.setdefault(locator.runtime, {})[locator.value] = record.content_hash
        return result

    @staticmethod
    def replace_mcp_target_servers(
        lockfile: LockFile,
        target_servers: dict[str, list[str]],
        *,
        native_hashes: dict[str, dict[str, str]] | None = None,
    ) -> None:
        """Update the MCP compatibility view and invalidate its projection.

        *native_hashes* controls content-hash retention on MCP URI rows:

        * ``None`` (default) — carry forward existing hashes for coordinates
          that survive in *target_servers*.  This is the backward-compatible
          path; no hash data is created or destroyed.
        * Explicit ``dict[runtime, dict[server, sha256-envelope]]`` — the map
          is authoritative.  Coordinates present in the map receive the
          supplied hash (validated); coordinates absent from the map receive
          ``None``.  No fallback to prior hashes.
        """
        lockfile.mcp_target_servers = {
            runtime: list(servers) for runtime, servers in target_servers.items()
        }
        lockfile._mcp_target_servers_present = True
        DeploymentLedgerCodec.refresh_from_legacy(lockfile)
        if native_hashes is not None:
            DeploymentLedgerCodec._apply_mcp_native_hashes(lockfile, native_hashes)

    @staticmethod
    def replace_lsp_target_servers(
        lockfile: LockFile,
        target_servers: dict[str, list[str]],
    ) -> None:
        """Update the LSP compatibility view and invalidate its projection."""
        lockfile.lsp_target_servers = {
            runtime: list(servers) for runtime, servers in target_servers.items()
        }
        lockfile._lsp_target_servers_present = True
        DeploymentLedgerCodec.refresh_from_legacy(lockfile)

    @staticmethod
    def _apply_mcp_native_hashes(
        lockfile: LockFile,
        native_hashes: dict[str, dict[str, str]],
    ) -> None:
        """Replace MCP URI content hashes from an authoritative map.

        Coordinates present in *native_hashes* receive the supplied hash
        (validated — malformed envelopes are rejected, stored as ``None``).
        Coordinates absent from the map receive ``None``.
        """
        records = dict(lockfile.deployment_ledger.records)
        changed = False
        for key, record in list(records.items()):
            locator = record.locator
            if (
                locator.kind is not LocatorKind.URI
                or locator.target != "mcp"
                or locator.runtime is None
            ):
                continue
            supplied = (native_hashes.get(locator.runtime) or {}).get(locator.value)
            new_hash = supplied if _is_canonical_bundle_hash(supplied) else None
            if new_hash != record.content_hash:
                records[key] = DeploymentRecord(
                    locator=locator,
                    owners=record.owners,
                    active_owner=record.active_owner,
                    content_hash=new_hash,
                )
                changed = True
        if changed:
            lockfile.deployment_ledger = DeploymentLedger(records=records)

    @staticmethod
    def replace_context_local_files(context: Any, files: list[str]) -> None:
        """Route transitional install-context ownership mutation through one owner."""
        context.local_deployed_files = list(files)

    @staticmethod
    def refresh_from_legacy(lockfile: LockFile) -> None:
        """Rebuild canonical rows after a compatibility view mutates in place."""
        prior_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
        prior_bundle_paths = DeploymentLedgerCodec.local_bundle_paths(lockfile)
        DeploymentLedgerCodec._rebuild_from_legacy(
            lockfile,
            prior_bundle_paths,
            prior_ledger=prior_ledger,
        )

    @staticmethod
    def invalidate_legacy_projection(
        lockfile: LockFile,
        prior_bundle_paths: frozenset[str] | None = None,
    ) -> None:
        """Invalidate compatibility rows while preserving imperative provenance."""
        if prior_bundle_paths is None:
            prior_bundle_paths = DeploymentLedgerCodec.local_bundle_paths(lockfile)
        prior_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
        # URI write receipts cannot be reconstructed from legacy views.
        has_uri_receipts = any(
            r.locator.kind is LocatorKind.URI and r.content_hash is not None
            for r in prior_ledger.records.values()
        )
        lockfile.deployment_ledger = DeploymentLedger(records={})
        lockfile._deployments_present = False
        if prior_bundle_paths or has_uri_receipts:
            DeploymentLedgerCodec._rebuild_from_legacy(
                lockfile, prior_bundle_paths, prior_ledger=prior_ledger
            )

    @staticmethod
    def rename_local_deployed_path(
        lockfile: LockFile,
        old_value: str,
        new_value: str,
    ) -> None:
        """Rename one local path while preserving imperative provenance."""
        if old_value not in lockfile.local_deployed_files:
            return
        prior_bundle_paths = DeploymentLedgerCodec.local_bundle_paths(lockfile)
        prior_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
        was_local_bundle = old_value in prior_bundle_paths
        lockfile.local_deployed_files = [
            value for value in lockfile.local_deployed_files if value != old_value
        ]
        if new_value not in lockfile.local_deployed_files:
            lockfile.local_deployed_files.append(new_value)
        if old_value in lockfile.local_deployed_file_hashes:
            old_hash = lockfile.local_deployed_file_hashes.pop(old_value)
            lockfile.local_deployed_file_hashes.setdefault(new_value, old_hash)
        if was_local_bundle:
            renamed_bundle_paths = (prior_bundle_paths - {old_value}) | {new_value}
            DeploymentLedgerCodec._rebuild_from_legacy(
                lockfile,
                frozenset(renamed_bundle_paths),
                prior_ledger=prior_ledger,
            )
        else:
            DeploymentLedgerCodec.invalidate_legacy_projection(lockfile, prior_bundle_paths)

    @staticmethod
    def _rebuild_from_legacy(
        lockfile: LockFile,
        prior_bundle_paths: frozenset[str],
        *,
        prior_ledger: DeploymentLedger | None = None,
        preferred_locators: dict[str, DeploymentLocator] | None = None,
    ) -> None:
        """Rebuild compatibility rows and restore surviving bundle provenance."""
        lockfile.deployment_ledger = DeploymentLedger(records={})
        lockfile._deployments_present = False
        lockfile.deployment_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
        lockfile._deployments_present = True
        if prior_ledger is not None:
            previous_by_locator = {
                (
                    record.locator.kind,
                    (record.locator.target if record.locator.kind == LocatorKind.URI else None),
                    record.locator.value,
                    record.locator.runtime,
                    record.locator.scope,
                ): record
                for record in prior_ledger.records.values()
            }
            records: dict[str, DeploymentRecord] = {}
            for record in lockfile.deployment_ledger.records.values():
                locator = record.locator
                previous = previous_by_locator.get(
                    (
                        locator.kind,
                        locator.target if locator.kind == LocatorKind.URI else None,
                        locator.value,
                        locator.runtime,
                        locator.scope,
                    )
                )
                preferred = (
                    (preferred_locators or {}).get(locator.value)
                    if locator.kind == LocatorKind.PROJECT_RELATIVE
                    else None
                )
                if previous is None and preferred is None:
                    records[locator.key] = record
                    continue
                active_owner = (
                    previous.active_owner
                    if previous is not None and previous.active_owner in record.owners
                    else record.active_owner
                )
                # URI locators (MCP/LSP servers) are re-synthesized with
                # content_hash=None by from_lockfile().  Carry forward the
                # prior hash so a rebuild cycle does not silently drop a
                # baseline recorded by a successful native write.
                content_hash = record.content_hash
                if (
                    content_hash is None
                    and previous is not None
                    and previous.content_hash is not None
                    and locator.kind is LocatorKind.URI
                ):
                    content_hash = previous.content_hash
                preserved = DeploymentRecord(
                    locator=preferred or previous.locator,
                    owners=record.owners,
                    active_owner=active_owner,
                    content_hash=content_hash,
                )
                records[preserved.locator.key] = preserved
            lockfile.deployment_ledger = DeploymentLedger(records=records)
        surviving_paths = prior_bundle_paths.intersection(lockfile.local_deployed_files)
        DeploymentLedgerCodec._mark_local_bundle_paths(lockfile, surviving_paths)

    @staticmethod
    def _mark_local_bundle_paths(
        lockfile: LockFile,
        bundle_paths: frozenset[str],
    ) -> None:
        """Mark existing ledger rows as hash-audited imperative bundle output."""
        if not bundle_paths:
            return
        records = dict(lockfile.deployment_ledger.records)
        marked_paths: set[str] = set()
        for key, record in records.items():
            path = DeploymentLedgerCodec.legacy_value(record.locator)
            if path not in bundle_paths:
                continue
            _require_local_bundle_hash(path, record.content_hash)
            owners = tuple(owner for owner in record.owners if owner != _LOCAL_BUNDLE_OWNER)
            records[key] = DeploymentRecord(
                locator=record.locator,
                owners=(*owners, _LOCAL_BUNDLE_OWNER),
                active_owner=_LOCAL_BUNDLE_OWNER,
                content_hash=record.content_hash,
            )
            marked_paths.add(path)
        missing = bundle_paths - marked_paths
        if missing:
            raise ValueError(f"Local bundle deployment rows are missing: {sorted(missing)}")
        lockfile.deployment_ledger = DeploymentLedger(records=records)
        lockfile._deployments_present = True

    @staticmethod
    def locator_for_path(
        path: Path | str,
        *,
        project_root: Path,
        target: TargetProfile,
        runtime: str | None = None,
        scope: InstallScope,
    ) -> DeploymentLocator:
        """Encode a path without forcing external roots into project strings."""
        scope_value = getattr(scope, "value", str(scope))
        if isinstance(path, str) and "://" in path:
            return DeploymentLocator(
                kind=LocatorKind.URI,
                target=target.name,
                value=path,
                runtime=runtime,
                scope=scope_value,
            )

        candidate = Path(path).resolve()
        root = project_root.resolve()
        try:
            project_path = ensure_path_within(candidate, root)
        except PathTraversalError:
            project_path = None
        if project_path is not None:
            return DeploymentLocator(
                kind=LocatorKind.PROJECT_RELATIVE,
                target=target.name,
                value=portable_relpath(project_path, root),
                runtime=runtime,
                scope=scope_value,
            )

        deploy_root = target.managed_deploy_root
        if deploy_root is not None:
            resolved_deploy_root = deploy_root.resolve()
            try:
                target_path = ensure_path_within(candidate, resolved_deploy_root)
            except PathTraversalError:
                target_path = None
            if target_path is not None:
                return DeploymentLocator(
                    kind=LocatorKind.TARGET_RELATIVE,
                    target=target.name,
                    value=portable_relpath(target_path, resolved_deploy_root),
                    runtime=runtime,
                    scope=scope_value,
                )

        raise RuntimeError(
            f"Cannot encode deployment path {candidate!r}: path is outside "
            "the project and target managed roots."
        )

    @staticmethod
    def resolve_locator(
        locator: DeploymentLocator,
        *,
        project_root: Path,
        target: TargetProfile,
    ) -> Path | str:
        """Resolve a persisted locator through its owning target profile."""
        if locator.kind == LocatorKind.URI:
            return locator.value
        if locator.kind == LocatorKind.PROJECT_RELATIVE:
            return ensure_path_within(project_root / locator.value, project_root)
        deploy_root = target.managed_deploy_root
        if deploy_root is None:
            raise RuntimeError(f"Target {target.name!r} has no managed root for {locator.key}")
        return ensure_path_within(deploy_root / locator.value, deploy_root)

    @staticmethod
    def rows(ledger: DeploymentLedger) -> list[dict[str, Any]]:
        """Return deterministic additive lockfile rows."""
        rows: list[dict[str, Any]] = []
        for key in sorted(ledger.records):
            record = ledger.records[key]
            locator = record.locator
            rows.append(
                {
                    "kind": locator.kind.value,
                    "target": locator.target,
                    "value": locator.value,
                    "runtime": locator.runtime,
                    "scope": locator.scope,
                    "owners": list(record.owners),
                    "active_owner": record.active_owner,
                    "content_hash": record.content_hash,
                }
            )
        return rows

    @staticmethod
    def from_rows(rows: Any) -> DeploymentLedger:
        """Parse additive rows, rejecting malformed entries."""
        DeploymentLedgerCodec.validate_rows(rows)
        records: dict[str, DeploymentRecord] = {}
        for row in rows:
            owners = tuple(str(owner) for owner in row.get("owners", ()) if owner)
            active_owner = str(row.get("active_owner") or "")
            locator = DeploymentLocator(
                kind=LocatorKind(str(row["kind"])),
                target=str(row["target"]),
                value=str(row["value"]),
                runtime=str(row["runtime"]) if row.get("runtime") is not None else None,
                scope=str(row["scope"]),
            )
            records[locator.key] = DeploymentRecord(
                locator=locator,
                owners=owners,
                active_owner=active_owner,
                content_hash=(
                    str(row["content_hash"]) if row.get("content_hash") is not None else None
                ),
            )
        return DeploymentLedger(records=records)

    @staticmethod
    def validate_rows(rows: Any) -> None:
        """Reject any malformed deployment row before ledger construction."""
        if not isinstance(rows, list):
            raise ValueError("Lockfile deployments must be a list")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"Lockfile deployment row {index} must be a mapping")
            try:
                LocatorKind(str(row["kind"]))
                target = row["target"]
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

    @staticmethod
    def _add_legacy_paths(
        records: dict[str, DeploymentRecord],
        owner: str,
        paths: list[str],
        hashes: dict[str, str],
    ) -> None:
        for value in paths:
            locator = DeploymentLedgerCodec._legacy_locator(value)
            prior = records.get(locator.key)
            owners = list(prior.owners) if prior else []
            if owner in owners:
                owners.remove(owner)
            owners.append(owner)
            records[locator.key] = DeploymentRecord(
                locator=locator,
                owners=tuple(owners),
                active_owner=owner,
                content_hash=hashes.get(value) or (prior.content_hash if prior else None),
            )

    @staticmethod
    def _legacy_locator(value: str) -> DeploymentLocator:
        scope = DeploymentLedgerCodec.legacy_scope(value)
        if "://" in value:
            target = value.split("://", 1)[0].removesuffix("-db")
            kind = LocatorKind.URI
        else:
            target = next(
                (
                    name
                    for prefix, name in _LEGACY_USER_TARGET_PREFIXES.items()
                    if value.startswith(prefix)
                ),
                None,
            )
            if target is None:
                target = next(
                    (
                        name
                        for prefix, name in _LEGACY_TARGET_PREFIXES.items()
                        if value.startswith(prefix)
                    ),
                    UNATTRIBUTED_TARGET,
                )
            kind = LocatorKind.PROJECT_RELATIVE
        return DeploymentLocator(
            kind=kind,
            target=target,
            value=value,
            runtime=None,
            scope=scope,
        )

    @staticmethod
    def legacy_scope(value: str) -> str:
        """Return the install scope encoded by a legacy deployed-file path."""
        if any(value.startswith(prefix) for prefix in _LEGACY_USER_TARGET_PREFIXES):
            return "user"
        return "project"

    @staticmethod
    def legacy_value(locator: DeploymentLocator) -> str:
        """Return the canonical compatibility value for any locator kind."""
        return locator.value
