"""Read-only linking of one directly declared, installed self-contained skill."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

import yaml

from ..deps.lockfile import LockFile, resolve_lockfile_path_for_read
from ..models.apm_package import APMPackage
from ..models.dependency.reference import DependencyReference
from ..models.dependency.selection import (
    DependencySelectionStatus,
    parse_dependency_entry,
    select_manifest_dependency,
)
from ..utils.path_security import ensure_path_within, has_symlink_component
from ..utils.yaml_io import FrontmatterDocument, load_yaml_str, loads_frontmatter_document
from .models import ContractError, ContractLimits, ImportedSkill, LeafContract, SourceLocation


def _read_bytes(path: Path, *, maximum: int, root: Path) -> bytes:
    """Bound selected reads and reject symlinks, special files and observed drift."""
    try:
        ensure_path_within(path, root)
        if has_symlink_component(root, path):
            raise ValueError("Nested symlinks are unsupported.")
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError("Expected a bounded regular file.")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0)
        )
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise ValueError("Selected file changed before capture.")
            raw = stream.read(maximum + 1)
        after = path.stat()
        if (
            len(raw) > maximum
            or len(raw) != before.st_size
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise ValueError("Selected file changed during capture.")
        return raw
    except (OSError, ValueError) as exc:
        raise ContractError(
            f"Cannot read selected file {path.name}: {exc}",
            code="import_source",
            location=SourceLocation(path, 1),
        ) from exc


def _package(raw: bytes, root: Path) -> tuple[APMPackage, dict[str, Any]]:
    try:
        data = load_yaml_str(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Manifest must be a mapping.")
        package = APMPackage.from_mapping(
            data, package_path=root, source_path=root, create_config=False
        )
        return package, data
    except (ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        raise ContractError(
            f"Invalid selected manifest: {exc}",
            code="invalid_manifest",
            location=SourceLocation(root / "apm.yml", 1),
        ) from exc


def read_project_manifest(
    root: Path, limits: ContractLimits, *, allow_missing: bool = False
) -> tuple[APMPackage, dict[str, Any], str | None]:
    """Interpret project declarations through their no-config-write owner."""
    manifest = root / "apm.yml"
    if allow_missing and not manifest.exists() and not manifest.is_symlink():
        package = APMPackage.from_mapping(
            {"name": "contract-caller", "version": "0.0.0"},
            package_path=root,
            source_path=root,
            create_config=False,
        )
        return package, {}, None
    raw = _read_bytes(root / "apm.yml", maximum=limits.source_bytes, root=root)
    package, data = _package(raw, root)
    return package, data, hashlib.sha256(raw).hexdigest()


def _skill_document(path: Path, root: Path, limits: ContractLimits) -> FrontmatterDocument:
    raw = _read_bytes(path, maximum=limits.source_bytes, root=root)
    try:
        document = loads_frontmatter_document(raw, max_bytes=limits.source_bytes)
    except ValueError as exc:
        raise ContractError(
            f"Invalid installed skill frontmatter: {exc}",
            code="invalid_import",
            location=SourceLocation(path, getattr(exc, "line", 1), getattr(exc, "column", 1)),
        ) from exc
    if not document.body.strip():
        raise ContractError("Installed skill body is empty.", code="invalid_import")
    return document


def _self_contained(root: Path, limits: ContractLimits) -> None:
    """Reject companion content before calling the whole-package hash owner."""
    # .git is transport administration, not imported content; hashes exclude it.
    allowed = {"apm.yml", "SKILL.md", ".apm-pin"}
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.is_symlink():
                raise ContractError("Installed skill contains a symlink.", code="invalid_import")
            if entry.name == ".git" and entry.is_dir(follow_symlinks=False):
                # The hash owner traverses then excludes Git administration;
                # bound that otherwise-hidden traversal before delegating.
                pending = [Path(entry.path)]
                visited = 0
                while pending:
                    with os.scandir(pending.pop()) as metadata:
                        for item in metadata:
                            visited += 1
                            if visited > limits.resource_files:
                                raise ContractError(
                                    "Git metadata exceeds the import scan limit.",
                                    code="import_limit",
                                )
                            if item.is_symlink():
                                raise ContractError(
                                    "Installed Git metadata contains a symlink.",
                                    code="invalid_import",
                                )
                            if item.is_dir(follow_symlinks=False):
                                pending.append(Path(item.path))
                continue
            if entry.name not in allowed or not entry.is_file(follow_symlinks=False):
                raise ContractError(
                    "Only a self-contained root SKILL.md and apm.yml are supported; "
                    "companion resources are unsupported.",
                    code="unsupported_import",
                )
            if entry.stat(follow_symlinks=False).st_size > limits.source_bytes:
                raise ContractError(
                    "Installed skill package exceeds its byte limit.", code="import_limit"
                )


def resolve_installed_skills(
    contract: LeafContract,
    project_root: Path,
    package: APMPackage,
    *,
    limits: ContractLimits | None = None,
) -> tuple[tuple[ImportedSkill, ...], str | None]:
    """Link declared names through lock identity and canonical materialization.

    Local locks attest identity only. Git packages additionally require their
    existing locked package-content hash and current declared-reference check.
    No resolver/download/install/deployment lifecycle is invoked.
    """
    from ..drift import detect_ref_change
    from ..utils.content_hash import verify_package_hash

    limits = limits or ContractLimits()
    lock_path = resolve_lockfile_path_for_read(project_root, read_only=True)
    if not lock_path.exists():
        if contract.imports:
            raise ContractError(
                "Import requires an existing lockfile; install explicitly first.",
                code="missing_lock",
            )
        return (), None
    raw_lock = _read_bytes(lock_path, maximum=limits.file_bytes, root=project_root)
    lock_digest = hashlib.sha256(raw_lock).hexdigest()
    # Parse the exact captured bytes with the lockfile's semantic owner.
    try:
        lock = LockFile.from_yaml(raw_lock.decode("utf-8"))
    except (ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise ContractError("Existing lockfile is malformed.", code="invalid_lock") from exc
    if not contract.imports:
        return (), lock_digest
    name = contract.imports[0]
    location = contract.locations.get("imports", SourceLocation(contract.path, 1))
    declarations = list((package.dependencies or {}).get("apm", []))
    declarations.extend((package.dev_dependencies or {}).get("apm", []))
    if len(declarations) > 256:
        raise ContractError(
            "Too many direct dependencies for bounded import selection.", location=location
        )
    selected = select_manifest_dependency(name, declarations, lock)
    if selected.status == DependencySelectionStatus.AMBIGUOUS:
        raise ContractError(
            "Import declaration is ambiguous.", code="ambiguous_import", location=location
        )
    modules = project_root / "apm_modules"
    matches: list[tuple[DependencyReference, Path, FrontmatterDocument]] = []
    for declaration in declarations:
        dependency = parse_dependency_entry(declaration)
        if not dependency.is_local and dependency.source not in {None, "git"}:
            continue
        if dependency.is_virtual_file() or dependency.is_marketplace:
            continue
        if (
            selected.status == DependencySelectionStatus.MATCHED
            and declaration != selected.manifest_entry
        ):
            continue
        locked = lock.get_dependency(dependency.get_unique_key())
        if locked is None:
            if selected.status == DependencySelectionStatus.MATCHED or (
                dependency.is_local and Path(dependency.local_path or "").name == name
            ):
                raise ContractError(
                    "Selected import has no lock identity.", code="missing_lock", location=location
                )
            continue
        try:
            installed = locked.to_dependency_ref().get_install_path(modules)
            if has_symlink_component(project_root, installed):
                raise ValueError("Installed root is a symlink.")
            skill_path = installed / "SKILL.md"
            if not skill_path.exists():
                if selected.status == DependencySelectionStatus.MATCHED or (
                    dependency.is_local and Path(dependency.local_path or "").name == name
                ):
                    raise ContractError(
                        "Selected skill is not installed.", code="missing_import", location=location
                    )
                continue
            document = _skill_document(skill_path, installed, limits)
            # Explicit dependency identities win selection. Short skill/package
            # names are read from declared installations, never storage strings.
            declared_name = None
            if (installed / "apm.yml").exists():
                installed_package, _ = _package(
                    _read_bytes(installed / "apm.yml", maximum=limits.source_bytes, root=installed),
                    installed,
                )
                declared_name = installed_package.name
            if (
                selected.status == DependencySelectionStatus.MATCHED
                or document.metadata.get("name") == name
                or declared_name == name
            ):
                matches.append((dependency, installed, document))
        except (OSError, ValueError) as exc:
            if isinstance(exc, ContractError):
                raise
            raise ContractError(
                "Cannot safely select installed import.", location=location
            ) from exc
    if len(matches) != 1:
        raise ContractError(
            "Import must identify exactly one directly declared installed skill.",
            code="ambiguous_import" if matches else "missing_import",
            location=location,
        )
    dependency, installed, document = matches[0]
    locked = lock.get_dependency(dependency.get_unique_key())
    if locked is None:
        raise ContractError(
            "Selected lock identity disappeared.", code="missing_lock", location=location
        )
    if locked.depth != 1 or locked.resolved_by or locked.declaring_parent:
        raise ContractError("Transitive skill imports are unsupported.", location=location)
    if detect_ref_change(dependency, locked):
        raise ContractError(
            "Manifest and installed lock reference differ.", code="import_drift", location=location
        )
    locked_ref = locked.to_dependency_ref()
    if locked_ref.get_identity() != dependency.get_identity():
        raise ContractError(
            "Installed host or dependency identity differs.", code="import_drift", location=location
        )
    _self_contained(installed, limits)
    if (installed / "apm.yml").exists():
        installed_package, _ = _package(
            _read_bytes(installed / "apm.yml", maximum=limits.source_bytes, root=installed),
            installed,
        )
        if any((installed_package.dependencies or {}).values()) or any(
            (installed_package.dev_dependencies or {}).values()
        ):
            raise ContractError(
                "Imported skills with dependencies are unsupported.", location=location
            )
    if dependency.is_local:
        verified_hash = None
        assurance = "observed-local-source"
    else:
        if not locked.resolved_commit or not locked.content_hash:
            raise ContractError(
                "Git import needs a locked commit and package hash.", location=location
            )
        if not verify_package_hash(installed, locked.content_hash):
            raise ContractError(
                "Installed package hash does not match its lock.",
                code="import_drift",
                location=location,
            )
        verified_hash = locked.content_hash
        assurance = "locked-package-hash"
    # Catch selected source replacement during manifest/integrity observations.
    if (
        _read_bytes(installed / "SKILL.md", maximum=limits.source_bytes, root=installed)
        != document.raw
    ):
        raise ContractError(
            "Imported skill changed during planning.", code="import_drift", location=location
        )
    return (
        ImportedSkill(
            name=name,
            source_path=installed / "SKILL.md",
            content=document.raw.decode("utf-8"),
            source_digest=hashlib.sha256(document.raw).hexdigest(),
            lock_identity=locked.get_unique_key(),
            resolved_commit=locked.resolved_commit,
            verified_package_hash=verified_hash,
            assurance=assurance,
        ),
    ), lock_digest
