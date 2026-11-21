"""Lockfile codec for canonical deployment ownership state."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
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
from apm_cli.core.legacy_lock_evidence import legacy_bytes_owned, legacy_lock_hashes
from apm_cli.core.remediation import DEPLOYMENT_OWNERS_INVALID
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
)
from apm_cli.utils.paths import portable_relpath

if TYPE_CHECKING:
    from apm_cli.core.scope import InstallScope
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.cleanup import DeploymentEvidence
    from apm_cli.integration.skill_adoption import LockedSkillSources
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
}
_LEGACY_USER_TARGET_PREFIXES = {
    ".copilot/": "copilot",
}
_LOCAL_BUNDLE_OWNER = "local-bundle"
UNATTRIBUTED_TARGET = "legacy"
DEPLOYMENT_OWNER_REMEDIATION = DEPLOYMENT_OWNERS_INVALID.sentence()
LOCAL_STATE_DIR = ".apm-local"
"""Per-deployment-root directory for local, never-shared APM state."""
_SHA256_PREFIX = "sha256:"
_LOWER_HEX = frozenset("0123456789abcdef")
# Linux (btrfs subvolumes) and macOS can report a new st_dev for the same root
# after a reboot or remount. Windows root identity is intentionally unchanged.
_ROOT_DEVICE_REBIND_SUPPORTED = sys.platform != "win32"
# Receipts examined per command; collection is incremental across commands.
_RECEIPT_COLLECTION_LIMIT = 256


def _receipt_key(root: str, scope: str) -> str:
    """The receipt file stem for a project *root* (as recorded) and *scope*."""
    return hashlib.sha256(f"{Path(root)}|{scope}".encode()).hexdigest()


def _next_receipt_batch(directory: Path, limit: int) -> list[Path]:
    """The next *limit* receipts after the cursor, wrapping, then advance it.

    Each command resumes where the previous one stopped, so retained receipts
    never hide one behind them. The cursor exists only while there are more
    receipts than one batch examines.
    """
    receipts = sorted(directory.glob("*.json"))
    cursor = directory / ".collection-cursor"
    last = ""
    with contextlib.suppress(OSError, UnicodeDecodeError):
        if not cursor.is_symlink():
            last = cursor.read_text(encoding="ascii").strip()
    start = next((i for i, path in enumerate(receipts) if path.name > last), 0)
    batch = (receipts[start:] + receipts[:start])[:limit]
    if len(receipts) > limit and not cursor.is_symlink():
        with contextlib.suppress(OSError):
            cursor.write_text(batch[-1].name + "\n", encoding="ascii")
    return batch


def retire_receipts_of_missing_projects(limit: int = _RECEIPT_COLLECTION_LIMIT) -> int:
    """Best effort: delete receipts whose recorded project root is gone (#4176).

    Receipts are not packages: this never selects package removals, which
    stay with install/orphans.py.

    A receipt goes only when its file name is the key of the root and scope it
    records, that root is absolute and no longer exists, and the root's parent
    directory does (so an unmounted volume never looks deleted). Any doubt
    keeps the receipt; no error reaches the command. Returns the count removed.
    """
    from apm_cli.core.scope import get_apm_home

    removed = 0
    try:
        directory = get_apm_home() / "deployment-receipts"
        if directory.is_symlink() or not directory.is_dir():
            return 0
        for path in _next_receipt_batch(directory, limit):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                root, scope = data["root"], data["scope"]
                if not isinstance(root, str) or not isinstance(scope, str):
                    continue
                recorded = Path(root)
                if (
                    path.stem == _receipt_key(root, scope)
                    and recorded.is_absolute()
                    and not os.path.lexists(recorded)
                    and recorded.parent.is_dir()
                ):
                    path.unlink()
                    removed += 1
            except (OSError, ValueError, KeyError, TypeError):
                continue
    except OSError:
        return removed
    return removed


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


def materialization_evidence_paths(project_root: Path, scope: InstallScope) -> tuple[Path, Path]:
    """Return ``(OS-user receipt file, checkout identity file)`` for *project_root*."""
    from apm_cli.core.scope import get_apm_home

    root = project_root.resolve()
    key = _receipt_key(root.as_posix(), scope.value)
    state_path = get_apm_home() / "deployment-receipts" / f"{key}.json"
    return state_path, root / LOCAL_STATE_DIR / "installation"


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
        self.state_path, self.identity_path = materialization_evidence_paths(self.root, scope)
        # Containment is anchored one level above APM_HOME (``$HOME`` by
        # default); for a shallow custom root the symlink check is the guard.
        self._receipts_base = self.state_path.parent.parent.parent
        self._records: dict[str, DeploymentRecord] = {}
        self._destinations: dict[str, str] = {}
        self._bindings: dict[str, list[str]] = {}
        self._written: dict[str, str] = {}
        self._kept: set[str] = set()
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
        # Update-only locked-version sources for receipt-less skill adoption (#3625).
        self.locked_sources: LockedSkillSources | None = None
        # The install's locked-render evidence for receipt-less overwrites (#4360).
        self.render_evidence: DeploymentEvidence | None = None
        # Managed files kept because nothing proved APM wrote them: (owner, path).
        self.kept_overwrites: list[tuple[str, Path]] = []
        # Kept files a pre-0.37 lock proves edited, by resolved path: "drift"
        # when the package brings the recorded bytes, else "blocked" (#4398).
        self.kept_edit_kinds: dict[str, str] = {}
        # Unedited pre-0.37 renders a frozen replay adopted as they are (#4398).
        self.legacy_adopted = 0
        # A frozen replay verifies; it never migrates pre-0.37 renders (#4398).
        self.verify_only = False
        self._legacy_hashes = legacy_lock_hashes(lockfile)
        self._load()

    def _validate_paths(self) -> None:
        for base, path in (
            (self._receipts_base, self.state_path),
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

    def _root_stat(self) -> os.stat_result:
        return self.root.stat()

    def _root_identity(self) -> list[int]:
        metadata = self._root_stat()
        return [metadata.st_dev, metadata.st_ino]

    def _root_birth_time(self) -> float | None:
        """Return the root's creation time where ``stat`` reports one (macOS, BSD)."""
        if not _ROOT_DEVICE_REBIND_SUPPORTED:
            return None
        return getattr(self._root_stat(), "st_birthtime", None)

    def _root_device_changed_only(self, data: Mapping[str, Any]) -> bool:
        """Accept the same root whose device number alone changed after a reboot or remount.

        Path, scope and installation UUID are matched by the caller. The inode
        must match too, and so must the recorded birth time whenever the receipt
        carries one. A recreated root or a copied ``.apm-local`` still fails.
        Receipts written before birth times were recorded bind on inode, path
        and installation UUID alone, as on platforms without a birth time.
        """
        recorded = data["root_identity"]
        current = self._root_identity()
        if (
            not _ROOT_DEVICE_REBIND_SUPPORTED
            or not isinstance(recorded, list)
            or len(recorded) != 2
            or not all(type(value) is int for value in recorded)
            or recorded[0] == current[0]
            or recorded[1] != current[1]
        ):
            return False
        if "root_birth_time" not in data:
            return True
        return data["root_birth_time"] == self._root_birth_time()

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
                or (
                    data["root_identity"] != self._root_identity()
                    and not self._root_device_changed_only(data)
                )
            ):
                raise ValueError("installation binding does not match")
            # A device-only change is rebound by the next writing session's flush.
            rebind = self._root_identity() != data["root_identity"]
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
            self._dirty = rebind
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

    def _write_locator(
        self, path: Path, targets: Sequence[TargetProfile]
    ) -> DeploymentLocator | None:
        """Locate a final-rendered write of any file primitive inside a target's roots."""
        locator = self._locator(path, targets)
        if locator is not None:
            return locator
        for target in targets:
            roots = [target.deploy_path(self.root)]
            if target.resolved_deploy_root is None:
                roots.extend(
                    self.root / mapping.deploy_root
                    for mapping in target.primitives.values()
                    if mapping.deploy_root
                )
            for root in roots:
                try:
                    ensure_path_within(path, root)
                except PathTraversalError:
                    continue
                return DeploymentLedgerCodec.locator_for_path(
                    path, project_root=self.root, target=target, scope=self.scope
                )
        return None

    @staticmethod
    def receipt_owner(package_info: Any) -> str:
        """Return the ownership key receipts record for one deployed package."""
        dependency_ref = getattr(package_info, "dependency_ref", None)
        return dependency_ref.get_unique_key() if dependency_ref is not None else "."

    def receipt_hashes(self) -> dict[str, str]:
        """Map each receipted physical path to the hash APM wrote there (#3701)."""
        return {
            physical: content_hash
            for physical, key in self._physical_keys.items()
            if (content_hash := self._records[key].content_hash) is not None
        }

    def receipt_held(self, path: Path) -> bool:
        """Return whether *path* still holds the bytes this installation receipted there."""
        record = self._records.get(self._physical_keys.get(path.resolve().as_posix(), ""))
        return record is not None and self._holds(path, record)

    def keep(self, path: Path, targets: Sequence[TargetProfile]) -> None:
        """Record a declared member this run deliberately left on disk unwritten."""
        from apm_cli.install.deployed_paths import deployed_path_entry

        self._kept.add(deployed_path_entry(path, self.root, targets))

    def kept_values(self) -> frozenset[str]:
        """Return lock values of declared members this run kept; cleanup retains them."""
        return frozenset(self._kept)

    def baseline(self, path: Path, targets: Sequence[TargetProfile]) -> str | None:
        """Return a prior rendered hash only for a currently authorized physical member.

        A receipt is trusted only when it is bound to the current lock claim
        (same owner and claim signature) and either the file still holds the
        receipted bytes or the lock records the receipted hash for the path.
        A receipt written under another checkout's lock therefore never
        vouches for, or refuses, different bytes now on disk. The lock can
        only withdraw trust here; it never grants it.
        """
        return self._trusted_hash(path, targets, self._locator)

    def overwrite_authorized(
        self, path: Path, claim: str, targets: Sequence[TargetProfile]
    ) -> bool:
        """Whether replacing an existing managed file has deployment evidence (#4360).

        This checkout's trusted receipt whose bytes the file still holds, or its
        bytes equal to what the locked versions render. A lockfile hash alone
        never authorizes it; an unreadable file is never overwritten.
        """
        from apm_cli.integration.cleanup import AUTHORIZED
        from apm_cli.utils.content_hash import compute_file_hash

        trusted = self._trusted_hash(path, targets, self._write_locator)
        try:
            if trusted is not None and compute_file_hash(path) == trusted:
                return True
        except OSError:
            return False
        if not self.verify_only and self.legacy_owned(path, claim):
            return True
        evidence = self.render_evidence
        return (
            evidence is not None
            and evidence.render_verdict(claim, path, rendered_as=None) == AUTHORIZED
        )

    def receipted_hash(self, path: Path, targets: Sequence[TargetProfile]) -> str | None:
        """The hash this checkout's receipt binds for *path*, else ``None`` (#4360)."""
        return self._trusted_hash(path, targets, self._write_locator)

    def legacy_recorded_hash(self, claim: str) -> str | None:
        """The hash a pre-0.37 lock recorded for *claim*, else ``None`` (#4398)."""
        return self._legacy_hashes.get(claim.replace("\\", "/"))

    def legacy_owned(self, path: Path, claim: str) -> bool:
        """Whether a pre-0.37 lock proves APM wrote *path*'s current bytes (#4398)."""
        return legacy_bytes_owned(path, self.legacy_recorded_hash(claim))

    def _trusted_hash(
        self,
        path: Path,
        targets: Sequence[TargetProfile],
        locate: Callable[[Path, Sequence[TargetProfile]], DeploymentLocator | None],
    ) -> str | None:
        from apm_cli.install.deployed_paths import deployed_path_entry

        locator = locate(path, targets)
        if locator is None:
            return None
        key = self._physical_keys.get(path.resolve().as_posix())
        record = self._records.get(key)
        if record is None:
            return None
        from apm_cli.integration.targets import KNOWN_TARGETS, scoped_profile_or_none

        recorded_target = next(
            (target for target in targets if target.name == record.locator.target), None
        )
        if recorded_target is None:
            profile = KNOWN_TARGETS.get(record.locator.target)
            recorded_target = (
                scoped_profile_or_none(profile, user_scope=self.scope.value == "user")
                if profile
                else None
            )
        if recorded_target is None or locate(path, [recorded_target]) != record.locator:
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
        claimed = {claim.content_hash for claim in self._claims[value]} - {None}
        if record.content_hash not in claimed and not self._holds(path, record):
            return None
        return record.content_hash

    @staticmethod
    def _holds(path: Path, record: DeploymentRecord) -> bool:
        """Return whether *path* still holds exactly the receipted bytes."""
        from apm_cli.utils.content_hash import compute_file_hash

        try:
            return path.is_file() and compute_file_hash(path) == record.content_hash
        except OSError:
            return False

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
        *,
        evidence_only: bool = False,
    ) -> None:
        """Accumulate rendered writes (or, *evidence_only*, earlier ones) for one flush."""
        from apm_cli.install.deployed_paths import deployed_path_entry

        if not written:
            return
        prepared: list[tuple[DeploymentRecord, str, str]] = []
        for path, content_hash in written.items():
            locator = self._write_locator(path, targets)
            if locator is None or path.is_symlink() or not _is_canonical_bundle_hash(content_hash):
                raise ValueError(f"Cannot record unverified materialization: {path}")
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
            if not evidence_only:
                self._written[key] = value
        self._dirty = True

    def written_entries(self) -> list[str]:
        """Return the deployed entries this session wrote, sorted."""
        return sorted(set(self._written.values()))

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
        birth_time = self._root_birth_time()
        if birth_time is not None:
            data["root_birth_time"] = birth_time
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


@contextmanager
def recorded_writes(
    skill_integrator: Any,
    integrator: Any,
    targets: Sequence[TargetProfile],
    package_info: Any,
) -> Iterator[None]:
    """Record the final-rendered writes of one integrator call, even on failure (#3701).

    Writes reported before the call (direct callers outside a package) belong
    to no package here and are discarded; files written before a failure are
    APM's bytes too and are recorded.
    """
    record_rendered_writes(None, integrator, targets=(), package_info=package_info)
    session = getattr(skill_integrator, "_materialization_session", None)
    integrator._overwrite_gate = OverwriteGate(
        session if isinstance(session, DeploymentMaterializationSession) else None,
        tuple(targets),
        DeploymentMaterializationSession.receipt_owner(package_info),
    )
    try:
        yield
    finally:
        integrator._overwrite_gate = None
        record_rendered_writes(
            skill_integrator, integrator, targets=targets, package_info=package_info
        )


@dataclass(frozen=True)
class OverwriteGate:
    """The evidence one integrator call needs to replace a deployed file (#4360).

    Without an installation-bound session nothing is authorized.
    """

    session: DeploymentMaterializationSession | None
    targets: tuple[TargetProfile, ...]
    owner: str

    def authorizes(self, path: Path, claim: str) -> bool:
        return self.session is not None and self.session.overwrite_authorized(
            path, claim, self.targets
        )

    def keep(self, path: Path, claim: str, *, edit: str | None = None) -> bool:
        """Record a kept dependency file; its prior lock entry stays (reported at finalize).

        *edit* classifies a file a pre-0.37 lock or a receipt proves edited:
        ``"drift"`` or ``"blocked"`` (#4398, RA-012).
        """
        if self.session is None or self.owner == ".":
            return False
        self.session.kept_overwrites.append((self.owner, path))
        if edit is not None:
            self.session.kept_edit_kinds[path.resolve().as_posix()] = edit
            # Keep the pre-0.37 proof across the lock upgrade (RA-012).
            legacy = self.session.legacy_recorded_hash(claim)
            if legacy is not None:
                self.session.record({path: legacy}, self.targets, self.owner, evidence_only=True)
        return True

    def adopt_legacy(self, path: Path, claim: str) -> bool:
        """A frozen install keeps an unedited pre-0.37 render as it is (#4398)."""
        adopted = (
            self.session is not None
            and self.session.verify_only
            and self.session.legacy_owned(path, claim)
        )
        if adopted:
            from apm_cli.utils.content_hash import compute_file_hash

            # The lock proves APM deployed these bytes: record them as such.
            self.session.record({path: compute_file_hash(path)}, self.targets, self.owner)
            self.session.legacy_adopted += 1
        return adopted

    def legacy_edit(self, path: Path, claim: str, rendered_hash: str) -> str | None:
        """Classify a pre-0.37 managed file whose bytes differ from its lock hash.

        ``"drift"`` when the package renders the recorded bytes (a user edit
        with nothing new to deploy), ``"blocked"`` when it renders new bytes.
        """
        from apm_cli.utils.content_hash import compute_file_hash

        recorded = self.session.legacy_recorded_hash(claim) if self.session else None
        if recorded is None or compute_file_hash(path) == recorded:
            return None
        return "drift" if rendered_hash == recorded else "blocked"

    @property
    def user_scope(self) -> bool:
        return self.session is not None and self.session.scope.value == "user"


def record_rendered_writes(
    skill_integrator: Any,
    integrator: Any,
    *,
    targets: Sequence[TargetProfile],
    package_info: Any,
) -> None:
    """Record the final-rendered writes one integrator call reported as receipts (#3701).

    Always drains *integrator* so writes never leak into a later package. The
    skill integrator holds the installation-bound session for the whole
    integrate phase; outside it (drift replay, dry runs) nothing is recorded.
    Integrators report only real writes, never adopted or skipped files.
    """
    from apm_cli.utils.content_hash import compute_file_hash

    take = getattr(integrator, "take_rendered_writes", None)
    written = take() if callable(take) else None
    session = getattr(skill_integrator, "_materialization_session", None)
    if not isinstance(session, DeploymentMaterializationSession) or not isinstance(written, list):
        return
    session.record(
        {path: compute_file_hash(path) for path in written},
        targets,
        DeploymentMaterializationSession.receipt_owner(package_info),
    )


def record_bundle_receipts(
    project_root: Path,
    *,
    scope: InstallScope,
    targets: Sequence[TargetProfile],
    written: Sequence[str],
    lockfile: LockFile,
) -> None:
    """Record local deployment receipts for files a local bundle install wrote (#4223).

    Bundle files cannot be rendered from a locked source, so the receipt is
    their only deletion evidence (req-lk-024). *written* lists only files this
    install actually wrote, never adopted identical ones; *lockfile* already
    holds their claims, so each receipt binds to the committed local-bundle
    claim. A file no target root contains is not receipted.
    """
    from apm_cli.utils.content_hash import compute_file_hash

    if not written:
        return
    session = DeploymentMaterializationSession(project_root, scope, lockfile)
    for claim in written:
        path = Path(claim)
        physical = path if path.is_absolute() else project_root / path
        if physical.is_symlink() or not physical.is_file():
            continue
        try:
            session.record({physical: compute_file_hash(physical)}, targets, _LOCAL_BUNDLE_OWNER)
        except ValueError:
            continue
    session.flush()


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


def trusted_receipt_baselines(
    project_root: Path,
    scope: InstallScope,
    lockfile: LockFile | None,
    paths: Sequence[Path],
    targets: Sequence[TargetProfile],
) -> dict[Path, str]:
    """Read-only: map each path to the hash this installation last wrote there.

    Only trusted receipts count (current ownership, bound installation).
    Invalid receipt state yields nothing; nothing is written or promoted.
    """
    try:
        session = DeploymentMaterializationSession(project_root, scope, lockfile)
    except (OSError, ValueError):
        return {}
    baselines: dict[Path, str] = {}
    for path in paths:
        try:
            baseline = session.baseline(path, targets)
        except (OSError, ValueError):
            continue
        if baseline is not None:
            baselines[path] = baseline
    return baselines


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
        bundle_sources: tuple[DeploymentLedger, ...] = (prior_ledger,)
        if provenance is not None:
            bundle_sources = (prior_ledger, provenance)
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
            bundle_scopes=DeploymentLedgerCodec._bundle_scopes(bundle_sources),
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
        *,
        scope: InstallScope,
        staged: Collection[str] = (),
        staged_hashes: Mapping[str, str] | None = None,
        staged_fallback_hashes: Mapping[str, str] | None = None,
    ) -> None:
        """Persist imperative bundle files without making them replayable source.

        *scope* is the install scope that deployed *files*; their rows record
        it, so a user-scope bundle keeps ``scope: user`` (#3995). It has no
        default: the owner never guesses a scope.

        *staged* is compile input under ``apm_modules/<slug>/.apm/`` (#3705):
        it is recorded in ``local_staged_files`` and never claimed. Every prior
        bundle-owned staging claim (pre-#3705 lockfiles) moves there too, even
        when this install skipped or no longer ships the file; bytes are kept.
        *staged_hashes* binds staged content for drift checks (#3718); a path
        this install did not restage keeps its prior hash, else its
        *staged_fallback_hashes* bundle digest.
        """
        from apm_cli.deps.lockfile import local_staging_slug

        prior_bundle_paths = DeploymentLedgerCodec.local_bundle_paths(lockfile)
        retired_paths = frozenset(
            path for path in prior_bundle_paths if local_staging_slug(path) is not None
        )
        prior_bundle_paths -= retired_paths
        retired_hashes = {
            path: digest
            for path, digest in lockfile.local_deployed_file_hashes.items()
            if path in retired_paths
        }
        for path in files:
            _require_local_bundle_hash(path, hashes.get(path))
        merged_files = sorted((set(lockfile.local_deployed_files) - retired_paths).union(files))
        merged_hashes = {
            path: digest
            for path, digest in lockfile.local_deployed_file_hashes.items()
            if path not in retired_paths
        }
        merged_hashes.update(hashes)
        DeploymentLedgerCodec.replace_legacy_owner(
            lockfile,
            ".",
            merged_files,
            merged_hashes,
        )

        scope_value = scope.value
        DeploymentLedgerCodec._mark_local_bundle_paths(
            lockfile,
            prior_bundle_paths.union(files),
            scopes=dict.fromkeys(files, scope_value),
        )
        lockfile.local_staged_files = sorted(
            set(lockfile.local_staged_files).union(staged, retired_paths)
        )
        lockfile.local_staged_file_hashes = {
            **(staged_fallback_hashes or {}),
            **retired_hashes,
            **lockfile.local_staged_file_hashes,
            **(staged_hashes or {}),
        }
        lockfile.retain_staged_hashes()
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
        bundle_scopes: Mapping[str, str] | None = None,
    ) -> None:
        """Rebuild compatibility rows and restore surviving bundle provenance.

        A surviving bundle row keeps the scope its unambiguous prior claim
        recorded (*bundle_scopes*, default: from *prior_ledger*); the path
        prefix alone cannot tell a user-scope ``.claude/`` path from a project
        one (#3995).
        """
        if bundle_scopes is None and prior_ledger is not None:
            bundle_scopes = DeploymentLedgerCodec._bundle_scopes((prior_ledger,))
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
        DeploymentLedgerCodec._mark_local_bundle_paths(
            lockfile,
            surviving_paths,
            scopes={
                path: scope
                for path, scope in (bundle_scopes or {}).items()
                if path in surviving_paths
            },
        )

    @staticmethod
    def _bundle_scopes(sources: Sequence[DeploymentLedger]) -> dict[str, str]:
        """Return the recorded scope of each bundle claim, from the first source holding it.

        A value claimed with more than one scope in one source is ambiguous
        and is left to the rebuild's own scope.
        """
        scopes: dict[str, str] = {}
        for source in sources:
            found: dict[str, set[str]] = {}
            for record in source.records.values():
                if record.active_owner == _LOCAL_BUNDLE_OWNER:
                    value = DeploymentLedgerCodec.legacy_value(record.locator)
                    found.setdefault(value, set()).add(record.locator.scope)
            for value, values in found.items():
                if len(values) == 1:
                    scopes.setdefault(value, next(iter(values)))
        return scopes

    @staticmethod
    def _mark_local_bundle_paths(
        lockfile: LockFile,
        bundle_paths: frozenset[str],
        *,
        scopes: Mapping[str, str] | None = None,
    ) -> None:
        """Mark existing ledger rows as hash-audited imperative bundle output.

        *scopes* records the install scope of a path's bundle claim.
        """
        if not bundle_paths:
            return
        records = dict(lockfile.deployment_ledger.records)
        marked_paths: set[str] = set()
        for key, record in list(records.items()):
            path = DeploymentLedgerCodec.legacy_value(record.locator)
            if path not in bundle_paths:
                continue
            _require_local_bundle_hash(path, record.content_hash)
            owners = tuple(owner for owner in record.owners if owner != _LOCAL_BUNDLE_OWNER)
            scope = (scopes or {}).get(path, record.locator.scope)
            locator = (
                record.locator
                if scope == record.locator.scope
                else replace(record.locator, scope=scope)
            )
            del records[key]
            records[locator.key] = DeploymentRecord(
                locator=locator,
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
                target=str(row["target"]),  # architecture-authority-exempt: ledger-row
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
    def user_lockfile_rows(lockfile: LockFile) -> list[dict[str, Any]]:
        """Return the rows a user lockfile (``$APM_HOME``) persists.

        In memory a row keeps its compatibility value and inferred scope;
        persisted, every row records scope ``user`` and a path under its
        target's user root outside HOME is target-relative. A form read from
        the lock is reused while it still maps back to the same row. Plain
        user-scoped keys are reserved first, so a target-relative form never
        displaces another row; it stays project-relative instead.
        """
        absolute = DeploymentLedgerCodec._absolute_values(lockfile)
        read_forms: dict[str, DeploymentLocator] = getattr(lockfile, "_user_row_forms", {})
        ordered = sorted(
            lockfile.deployment_ledger.records.values(),
            key=lambda record: (record.locator.scope != "user", record.locator.key),
        )
        reserved = {replace(record.locator, scope="user").key for record in ordered}
        records: dict[str, DeploymentRecord] = {}
        for record in ordered:
            plain = replace(record.locator, scope="user")
            options = [plain, record.locator]
            if plain.kind == LocatorKind.PROJECT_RELATIVE and plain.value in absolute:
                root = DeploymentLedgerCodec._user_root(plain.target)
                read_form = read_forms.get(record.locator.key)
                if (
                    read_form is not None
                    and read_form.kind == LocatorKind.TARGET_RELATIVE
                    and plain.value.endswith(f"/{read_form.value}")
                ):
                    root = plain.value[: -len(read_form.value) - 1]
                relative = plain.value.removeprefix(f"{root}/") if root else plain.value
                converted = replace(plain, kind=LocatorKind.TARGET_RELATIVE, value=relative)
                if relative != plain.value and converted.key not in reserved:
                    options.insert(0, converted)
            locator = next(option for option in options if option.key not in records)
            records[locator.key] = replace(record, locator=locator)
        return DeploymentLedgerCodec.rows(DeploymentLedger(records=records))

    @staticmethod
    def adopt_lockfile_rows(lockfile: LockFile | None, path: Path) -> LockFile | None:
        """Return *lockfile* read from *path*, a user lockfile's rows in memory form.

        Only the user lockfile (``$APM_HOME``, decided by
        :mod:`apm_cli.core.scope`) persists the user form; a project lockfile's
        recorded scope is evidence and reads unchanged.
        """
        from apm_cli.core.scope import is_user_scope_root

        if lockfile is not None and is_user_scope_root(path.parent):
            DeploymentLedgerCodec.adopt_user_lockfile_rows(lockfile)
        return lockfile

    @staticmethod
    def adopt_user_lockfile_rows(lockfile: LockFile) -> None:
        """Map rows read from a user lockfile back to their in-memory form.

        The inverse of :meth:`user_lockfile_rows`: a target-relative value
        resolves through the lock's own compatibility views, else the
        target's user root. A lock still carrying project-scope absolute rows
        reads unchanged.
        """
        absolute = DeploymentLedgerCodec._absolute_values(lockfile)
        resolved = DeploymentLedgerCodec._resolve_target_relative(
            lockfile.deployment_ledger, absolute
        )
        forms: dict[str, DeploymentLocator] = {}
        records: dict[str, DeploymentRecord] = {}
        for key, record in lockfile.deployment_ledger.records.items():
            locator = record.locator
            if locator.scope == "user":
                if key in resolved:
                    locator = replace(
                        locator, kind=LocatorKind.PROJECT_RELATIVE, value=resolved[key]
                    )
                bundle = record.active_owner == _LOCAL_BUNDLE_OWNER
                scope = "user" if bundle else DeploymentLedgerCodec.legacy_scope(locator.value)
                locator = replace(locator, scope=scope)
                forms[locator.key] = record.locator
            records[locator.key] = replace(record, locator=locator)
        lockfile.deployment_ledger = DeploymentLedger(records=records)
        lockfile._user_row_forms = forms

    @staticmethod
    def _resolve_target_relative(ledger: DeploymentLedger, absolute: set[str]) -> dict[str, str]:
        """Map each user target-relative row key to its absolute compatibility value.

        Per target: the path under the target's resolved user root wins, then
        the one candidate not already taken by another row, then the root path.
        """
        pending = {
            key: record.locator
            for key, record in ledger.records.items()
            if record.locator.scope == "user" and record.locator.kind == LocatorKind.TARGET_RELATIVE
        }
        roots = {key: DeploymentLedgerCodec._user_root(loc.target) for key, loc in pending.items()}
        resolved = {
            key: f"{roots[key]}/{loc.value}"
            for key, loc in pending.items()
            if roots[key] and f"{roots[key]}/{loc.value}" in absolute
        }
        for key, loc in sorted(pending.items()):
            if key in resolved:
                continue
            taken = set(resolved.values())
            matches = [v for v in absolute if v.endswith(f"/{loc.value}") and v not in taken]
            if len(matches) == 1:
                resolved[key] = matches[0]
            elif roots[key]:
                resolved[key] = f"{roots[key]}/{loc.value}"
        return resolved

    @staticmethod
    def persisted_rows(lockfile: LockFile, *, user_scope: bool) -> list[dict[str, Any]]:
        """Return the rows *lockfile* persists; *user_scope* selects the user form."""
        if user_scope:
            return DeploymentLedgerCodec.user_lockfile_rows(lockfile)
        return DeploymentLedgerCodec.rows(lockfile.deployment_ledger)

    @staticmethod
    def prepare_lockfile_write(lockfile: LockFile, existing: LockFile | None, path: Path) -> bool:
        """Return whether *path* is the user lockfile, carrying its read row forms.

        The user lockfile is the one in the user-scope manifest directory
        (``$APM_HOME``), the single answer of :mod:`apm_cli.core.scope`.
        """
        from apm_cli.core.scope import is_user_scope_root

        if not is_user_scope_root(path.parent):
            return False
        if existing is not None:
            if not hasattr(existing, "_user_row_forms"):
                DeploymentLedgerCodec.adopt_user_lockfile_rows(existing)
            lockfile._user_row_forms = {
                **existing._user_row_forms,
                **getattr(lockfile, "_user_row_forms", {}),
            }
        return True

    @staticmethod
    def _absolute_values(lockfile: LockFile) -> set[str]:
        """Return the absolute path values of a lockfile's compatibility views."""
        values = set(lockfile.local_deployed_files)
        for owner, dependency in lockfile.dependencies.items():
            if owner != ".":
                values.update(dependency.deployed_files)
        values.update(
            record.locator.value
            for record in lockfile.deployment_ledger.records.values()
            if record.locator.kind == LocatorKind.PROJECT_RELATIVE
        )
        return {
            value
            for value in values
            if "://" not in value and (value.startswith("/") or Path(value).is_absolute())
        }

    @staticmethod
    def _user_root(target_name: str) -> str | None:
        """Return the absolute user-scope managed root of *target_name*, if any."""
        from apm_cli.integration.targets import KNOWN_TARGETS, scoped_profile_or_none

        profile = KNOWN_TARGETS.get(target_name)
        scoped = scoped_profile_or_none(profile, user_scope=True) if profile else None
        root = scoped.managed_deploy_root if scoped is not None else None
        return root.resolve().as_posix() if root is not None else None

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
    def locator_in_scope(locator: DeploymentLocator, scope: str) -> bool:
        """Return whether a persisted deployment row belongs to *scope*.

        Rows rebuilt from compatibility views carry the scope
        :meth:`legacy_scope` infers from the path, which cannot tell a
        user-scope ``.claude/`` path from a project one. That inferred value
        is not scope evidence; only a recorded scope that differs from the
        inference places a row in another scope.
        """
        return locator.scope in (scope, DeploymentLedgerCodec.legacy_scope(locator.value))

    @staticmethod
    def legacy_value(locator: DeploymentLocator) -> str:
        """Return the canonical compatibility value for any locator kind."""
        return locator.value
