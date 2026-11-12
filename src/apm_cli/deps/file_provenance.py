"""Read-only reverse provenance for one deployed file (``apm view --file``).

Maps an exact deployed path back to the owners recorded in the canonical
deployment ledger. Ownership comes only from exact-path ledger records read
through :class:`DeploymentLedgerCodec`; it is never inferred from names,
directory prefixes or substrings. Current on-disk bytes are reported
separately from recorded provenance.

This module never writes: it reads the lockfile without legacy migration,
does not repair or adopt ownership, does not touch the network and does not
follow symlinks when checking current bytes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..core.deployment_ledger import DeploymentLedgerCodec
from ..core.deployment_state import DeploymentRecord, LocatorKind
from ..utils.content_hash import compute_file_hash
from ..utils.git_env import redact_git_diagnostic
from ..utils.path_security import has_symlink_component
from .lockfile import (
    LockedDependency,
    LockFile,
    LockfileFormatError,
    resolve_lockfile_path_for_read,
)

PROJECT_SELF_OWNER = "."
LOCAL_BUNDLE_OWNER = "local-bundle"
_SHA256_PREFIX = "sha256:"


class FileProvenanceError(Exception):
    """Invalid or unreadable provenance state; message names the next action."""


class OwnerKind(str, Enum):
    """Category of one recorded owner."""

    PROJECT_SELF = "project-self"
    LOCAL_BUNDLE = "local-bundle"
    DIRECT_DEPENDENCY = "direct-dependency"
    TRANSITIVE_DEPENDENCY = "transitive-dependency"
    UNRESOLVED = "unresolved"


class FileStatus(str, Enum):
    """Current on-disk state relative to the recorded deployed hash."""

    MATCHES = "matches"
    MODIFIED = "modified"
    MISSING = "missing"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class OwnerProvenance:
    """One recorded owner and the lock facts recorded for it."""

    key: str
    kind: OwnerKind
    active: bool
    name: str | None = None
    version: str | None = None
    source: str | None = None
    ref: str | None = None
    commit: str | None = None
    resolved_by: str | None = None
    local_source: bool = False


@dataclass(frozen=True)
class RecordProvenance:
    """One exact-path ledger record and the current state of its file."""

    target: str
    value: str
    owners: tuple[OwnerProvenance, ...]
    content_hash: str | None
    status: FileStatus
    status_detail: str = ""

    @property
    def active_owner(self) -> OwnerProvenance | None:
        return next((owner for owner in self.owners if owner.active), None)


@dataclass(frozen=True)
class FileProvenance:
    """Result of one reverse lookup."""

    query: str
    relative_path: str
    lockfile_path: Path
    legacy_records: bool
    records: tuple[RecordProvenance, ...] = field(default_factory=tuple)

    @property
    def recorded(self) -> bool:
        return bool(self.records)

    @property
    def has_resolved_owner(self) -> bool:
        return any(
            owner.kind is not OwnerKind.UNRESOLVED
            for record in self.records
            for owner in record.owners
        )


def _safe(text: str | None) -> str | None:
    """Return *text* with credentials and secrets redacted for display."""
    if not text:
        return None
    return redact_git_diagnostic(str(text))


def _dependency_source(dependency: LockedDependency) -> str | None:
    if dependency.is_local_source:
        location = dependency.anchored_local_path or dependency.local_path
        return _safe(f"local path {location}") if location else "local path (not recorded)"
    if dependency.source == "registry":
        return _safe(f"registry {dependency.resolved_url or dependency.repo_url}")
    host = f"{dependency.host}/" if dependency.host else ""
    path = (
        f"/{dependency.virtual_path}" if dependency.is_virtual and dependency.virtual_path else ""
    )
    return _safe(f"{host}{dependency.repo_url}{path}")


def _owner_provenance(owner: str, active: bool, lockfile: LockFile) -> OwnerProvenance:
    if owner == PROJECT_SELF_OWNER:
        return OwnerProvenance(key=owner, kind=OwnerKind.PROJECT_SELF, active=active)
    if owner == LOCAL_BUNDLE_OWNER:
        return OwnerProvenance(key=owner, kind=OwnerKind.LOCAL_BUNDLE, active=active)
    dependency = lockfile.dependencies.get(owner)
    if dependency is None:
        return OwnerProvenance(key=_safe(owner) or owner, kind=OwnerKind.UNRESOLVED, active=active)
    kind = (
        OwnerKind.TRANSITIVE_DEPENDENCY
        if dependency.resolved_by or (dependency.depth or 1) > 1
        else OwnerKind.DIRECT_DEPENDENCY
    )
    return OwnerProvenance(
        key=_safe(owner) or owner,
        kind=kind,
        active=active,
        name=dependency.name,
        version=dependency.version,
        source=_dependency_source(dependency),
        ref=_safe(dependency.resolved_ref or dependency.resolved_tag),
        commit=dependency.resolved_commit,
        resolved_by=_safe(dependency.resolved_by),
        local_source=dependency.is_local_source,
    )


def _current_status(
    base: Path, file_path: Path, content_hash: str | None
) -> tuple[FileStatus, str]:
    """Classify current bytes without following symlinks or adopting content."""
    if has_symlink_component(base, file_path):
        return FileStatus.UNVERIFIABLE, "path crosses a symlink, which is not followed"
    if not os.path.lexists(file_path):
        return FileStatus.MISSING, ""
    if not file_path.is_file():
        return FileStatus.UNVERIFIABLE, "not a regular file"
    if content_hash is None:
        return FileStatus.UNVERIFIABLE, "no deployed hash was recorded"
    if not content_hash.startswith(_SHA256_PREFIX):
        return FileStatus.UNVERIFIABLE, "recorded hash has an unrecognized format"
    try:
        actual = compute_file_hash(file_path)
    except OSError as exc:
        return FileStatus.UNVERIFIABLE, f"cannot read file ({exc.strerror or exc})"
    if actual == content_hash:
        return FileStatus.MATCHES, ""
    return FileStatus.MODIFIED, ""


def _lexical_absolute(query: str, cwd: Path) -> Path:
    candidate = Path(query).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return Path(os.path.normpath(candidate))


def _relative_to_any(path: Path, roots: tuple[Path, ...]) -> tuple[Path, str] | None:
    """Return the first ``(root, posix-relative)`` for which *path* is contained."""
    for root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if relative.parts and ".." not in relative.parts:
            return root, relative.as_posix()
    return None


def _roots(root: Path) -> tuple[Path, ...]:
    lexical = Path(os.path.normpath(root.absolute()))
    resolved = root.resolve()
    return (lexical,) if lexical == resolved else (lexical, resolved)


def _target_root(target: str, user_scope: bool) -> Path | None:
    """Return a target's managed root for target-relative records, if any."""
    from ..integration.targets import KNOWN_TARGETS

    profile = KNOWN_TARGETS.get(target)
    if profile is None:
        return None
    try:
        scoped = profile.for_scope(user_scope=user_scope)
    except Exception:
        return None
    return scoped.managed_deploy_root if scoped is not None else None


def _read_lockfile(lock_dir: Path, user_scope: bool) -> tuple[Path, LockFile]:
    lock_path = resolve_lockfile_path_for_read(lock_dir, read_only=True)
    if lock_path.is_symlink():
        raise FileProvenanceError(
            f"Refusing to read symlinked lockfile {lock_path}. "
            "Replace it with a regular file, then rerun 'apm view --file'."
        )
    if not lock_path.exists():
        raise FileProvenanceError(
            f"No lockfile found at {lock_path}. "
            + (
                "Run 'apm install -g' first, or omit -g to inspect project files."
                if user_scope
                else "Run 'apm install' first, or use -g to inspect user-scope files."
            )
        )
    try:
        lockfile = LockFile.read(lock_path)
    except LockfileFormatError as exc:
        raise FileProvenanceError(
            f"Cannot read deployment records: {_safe(str(exc))}. "
            "Fix or regenerate the lockfile with 'apm install', then retry."
        ) from exc
    except OSError as exc:
        raise FileProvenanceError(
            f"Cannot read lockfile {lock_path}: {exc.strerror or exc}. "
            "Check file permissions, then retry."
        ) from exc
    if lockfile is None:
        raise FileProvenanceError(f"No lockfile found at {lock_path}. Run 'apm install' first.")
    return lock_path, lockfile


def _record_provenance(
    record: DeploymentRecord, base: Path, relative: str, lockfile: LockFile
) -> RecordProvenance:
    owners = tuple(
        _owner_provenance(owner, owner == record.active_owner, lockfile) for owner in record.owners
    )
    status, detail = _current_status(base, base / relative, record.content_hash)
    return RecordProvenance(
        target=record.locator.target,
        value=record.locator.value,
        owners=owners,
        content_hash=record.content_hash,
        status=status,
        status_detail=detail,
    )


def lookup_file_provenance(
    query: str,
    *,
    lock_dir: Path,
    deploy_root: Path,
    user_scope: bool,
    cwd: Path | None = None,
) -> FileProvenance:
    """Return recorded owners for the exact deployed path *query*.

    Raises :class:`FileProvenanceError` for unreadable state or a path
    outside the selected scope's deploy root.
    """
    if not query or not query.strip():
        raise FileProvenanceError("--file requires a non-empty PATH.")
    absolute = _lexical_absolute(query, cwd if cwd is not None else Path.cwd())
    if absolute in _roots(deploy_root):
        raise FileProvenanceError(
            f"'{query}' is the deploy root itself. Pass the path of one deployed file."
        )
    lock_path, lockfile = _read_lockfile(lock_dir, user_scope)
    ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    project_match = _relative_to_any(absolute, _roots(deploy_root))

    records: list[RecordProvenance] = []
    target_roots: dict[str, Path | None] = {}
    for key in sorted(ledger.records):
        record = ledger.records[key]
        kind = record.locator.kind
        value = record.locator.value.rstrip("/")
        if kind is LocatorKind.PROJECT_RELATIVE:
            if project_match is None or value != project_match[1]:
                continue
            records.append(_record_provenance(record, project_match[0], value, lockfile))
        elif kind is LocatorKind.TARGET_RELATIVE:
            target = record.locator.target
            if target not in target_roots:
                target_roots[target] = _target_root(target, user_scope)
            root = target_roots[target]
            match = _relative_to_any(absolute, _roots(root)) if root is not None else None
            if match is None or value != match[1]:
                continue
            records.append(_record_provenance(record, match[0], value, lockfile))

    if project_match is None and not records:
        scope_hint = (
            "Omit -g to inspect project files."
            if user_scope
            else "Use -g to inspect user-scope files."
        )
        raise FileProvenanceError(
            f"'{query}' is outside the {'user' if user_scope else 'project'} deploy root "
            f"{deploy_root}. {scope_hint}"
        )

    relative = project_match[1] if project_match is not None else records[0].value
    return FileProvenance(
        query=query,
        relative_path=relative,
        lockfile_path=lock_path,
        legacy_records=not getattr(lockfile, "_deployments_present", False),
        records=tuple(records),
    )
