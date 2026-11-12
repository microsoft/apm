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

from ..core.deployment_ledger import _SHA256_PREFIX, DeploymentLedgerCodec
from ..core.deployment_state import DeploymentRecord, LocatorKind
from ..utils.content_hash import compute_file_hash
from ..utils.diagnostics import printable_ascii_text
from ..utils.git_env import redact_git_diagnostic
from ..utils.path_security import has_symlink_component
from .lockfile import _SELF_KEY as PROJECT_SELF_OWNER
from .lockfile import (
    LockedDependency,
    LockFile,
    LockfileFormatError,
    resolve_lockfile_path_for_read,
)

# The codec owns local-bundle semantics; this read-only view only labels the key.
LOCAL_BUNDLE_OWNER = "local-bundle"


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
    owner_violation: bool = False

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
    def incomplete(self) -> bool:
        """Whether any matched record names an owner the lockfile cannot resolve."""
        return any(record.owner_violation for record in self.records)


def _safe(text: str | None) -> str | None:
    """Return lockfile-sourced *text* redacted and stripped of control characters."""
    if not text:
        return None
    return printable_ascii_text(redact_git_diagnostic(str(text)))


def _dependency_source(dependency: LockedDependency) -> str | None:
    """Describe the source APM installs from: the replacement when one is selected."""
    if dependency.source == "registry" and dependency.replacement is None:
        return _safe(f"registry {dependency.resolved_url or dependency.repo_url}")
    selected = dependency.to_dependency_ref()
    suffix = " (replacement)" if dependency.replacement is not None else ""
    if dependency.is_local_source:
        location = selected.anchored_local_path or selected.local_path
        if not location:
            return f"local path (not recorded){suffix}"
        return _safe(f"local path {location}{suffix}")
    host = f"{selected.host}/" if selected.host else ""
    path = f"/{selected.virtual_path}" if selected.is_virtual and selected.virtual_path else ""
    return _safe(f"{host}{selected.repo_url}{path}{suffix}")


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
        name=_safe(dependency.name),
        version=_safe(dependency.version),
        source=_dependency_source(dependency),
        ref=_safe(dependency.resolved_ref or dependency.resolved_tag),
        commit=_safe(dependency.resolved_commit),
        resolved_by=_safe(dependency.resolved_by),
        local_source=dependency.is_local_source,
    )


def _current_status(
    base: Path, file_path: Path, content_hash: str | None
) -> tuple[FileStatus, str]:
    """Classify current bytes without following symlinks or adopting content."""
    # The deploy root itself counts: a symlinked $HOME or project root is not followed.
    if base.is_symlink() or has_symlink_component(base, file_path):
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


def _anchor_to_root(absolute: Path, deploy_root: Path) -> Path:
    """Map a logical path through symlinked parents onto the deploy root.

    ``Path.cwd()`` is physical, while a shell ``$PWD`` (or a symlinked
    workspace such as macOS ``/tmp``) is logical. Resolve only the parent
    directory so the queried leaf stays exact, and only when the lexical
    form is not already under the root.
    """
    roots = _roots(deploy_root)
    if _relative_to_any(absolute, roots) is not None:
        return absolute
    try:
        physical = Path(os.path.realpath(absolute.parent)) / absolute.name
    except (OSError, ValueError):
        return absolute
    return physical if _relative_to_any(physical, roots) is not None else absolute


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
    record: DeploymentRecord,
    base: Path,
    relative: str,
    lockfile: LockFile,
    owner_violation: bool,
) -> RecordProvenance:
    owners = tuple(
        _owner_provenance(owner, owner == record.active_owner, lockfile) for owner in record.owners
    )
    status, detail = _current_status(base, base / relative, record.content_hash)
    return RecordProvenance(
        target=_safe(record.locator.target) or "",
        value=record.locator.value,
        owners=owners,
        content_hash=record.content_hash,
        status=status,
        status_detail=detail,
        owner_violation=owner_violation,
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
    absolute = _anchor_to_root(
        _lexical_absolute(query, cwd if cwd is not None else Path.cwd()), deploy_root
    )
    if absolute in _roots(deploy_root):
        raise FileProvenanceError(
            f"'{query}' is the deploy root itself. Pass the path of one deployed file."
        )
    lock_path, lockfile = _read_lockfile(lock_dir, user_scope)
    ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    # Canonical rows carry their recorded scope; legacy-synthesized rows only infer it.
    canonical = bool(getattr(lockfile, "_deployments_present", False))
    scope_value = "user" if user_scope else "project"
    violations = {
        violation.locator.key
        for violation in DeploymentLedgerCodec.owner_reference_violations(lockfile, ledger=ledger)
    }
    project_match = _relative_to_any(absolute, _roots(deploy_root))

    matched: list[tuple[str, DeploymentRecord, tuple[Path, str]]] = []
    target_matches: dict[str, tuple[Path, str] | None] = {}
    for key, record in ledger.records.items():
        if canonical and record.locator.scope != scope_value:
            continue
        kind = record.locator.kind
        value = record.locator.value.rstrip("/")
        if kind is LocatorKind.PROJECT_RELATIVE:
            match = project_match
        elif kind is LocatorKind.TARGET_RELATIVE:
            target = record.locator.target
            if target not in target_matches:
                root = _target_root(target, user_scope)
                target_matches[target] = (
                    _relative_to_any(absolute, _roots(root)) if root is not None else None
                )
            match = target_matches[target]
        else:
            continue
        if match is not None and value == match[1]:
            matched.append((key, record, match))

    records = [
        _record_provenance(record, match[0], match[1], lockfile, record.locator.key in violations)
        for key, record, match in sorted(matched, key=lambda item: item[0])
    ]

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
        relative_path=printable_ascii_text(relative),
        lockfile_path=lock_path,
        legacy_records=not canonical,
        records=tuple(records),
    )
