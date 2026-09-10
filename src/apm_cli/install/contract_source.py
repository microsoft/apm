"""Acquire one contract package without activating or installing a project."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import yaml

from apm_cli.contracts.frontend import package_contract_path as _contract_path
from apm_cli.contracts.frontend import parse_contract
from apm_cli.contracts.imports import (
    _read_bytes,
    _self_contained,
    read_project_manifest,
    resolve_installed_skills,
)
from apm_cli.contracts.models import (
    ContractError,
    ContractLimits,
    ContractSource,
    LeafContract,
    Outcome,
)
from apm_cli.contracts.workspace import _check_names, _read, _write
from apm_cli.core.command_logger import InstallLogger
from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.lockfile import LockedDependency, LockFile, resolve_lockfile_path_for_read
from apm_cli.drift import build_download_ref, detect_ref_change
from apm_cli.install.contract_source_validation import (
    bounded_tree,
    package_dependency,
    source_hash,
    validate_reference,
)
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.selection import (
    DependencySelectionStatus,
    parse_dependency_entry,
    select_manifest_dependency,
)
from apm_cli.utils.content_hash import verify_package_hash
from apm_cli.utils.path_security import (
    ensure_path_within,
    has_symlink_component,
    safe_rmtree,
)
from apm_cli.utils.yaml_io import dump_yaml


def _read_lock(root: Path, limits: ContractLimits) -> LockFile | None:
    path = resolve_lockfile_path_for_read(root, read_only=True)
    if not path.exists():
        return None
    raw = _read_bytes(path, maximum=limits.file_bytes, root=root)
    try:
        return LockFile.from_yaml(raw.decode("utf-8"))
    except (ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        raise ContractError("Existing package lock is malformed.", code="invalid_lock") from exc


def _caller_source_pin(
    requested: DependencyReference, caller_root: Path, limits: ContractLimits
) -> tuple[LockFile, LockedDependency] | None:
    """Select the same direct caller lock authority for planning and execution."""
    from apm_cli.utils.github_host import is_full_commit_sha

    package, _, _ = read_project_manifest(caller_root, limits, allow_missing=True)
    lock = _read_lock(caller_root, limits)
    declarations = list((package.dependencies or {}).get("apm", []))
    declarations.extend((package.dev_dependencies or {}).get("apm", []))
    selection = select_manifest_dependency(str(requested), declarations, lock)
    if selection.status == DependencySelectionStatus.AMBIGUOUS:
        raise ContractError(
            "Caller source declaration is ambiguous. Select one direct declaration before retrying.",
            code="unresolved_source",
            outcome=Outcome.UNPROVEN,
        )
    if lock is None or selection.status != DependencySelectionStatus.MATCHED:
        return None
    declared = parse_dependency_entry(selection.manifest_entry)
    locked = lock.get_dependency(declared.get_unique_key())
    if (
        locked is None
        or locked.depth != 1
        or locked.resolved_by
        or locked.declaring_parent
        or not is_full_commit_sha(locked.resolved_commit)
        or not locked.content_hash
        or detect_ref_change(requested, locked)
        or detect_ref_change(declared, locked)
        or locked.to_dependency_ref().get_identity() != requested.get_identity()
    ):
        raise ContractError(
            "Remote source lacks an exact direct caller lock identity. "
            "Repair the matching caller declaration and lock before retrying.",
            code="unresolved_source",
            outcome=Outcome.UNPROVEN,
        )
    return lock, locked


def _installed_source(
    locked: LockedDependency, caller_root: Path, limits: ContractLimits
) -> Path | None:
    """Verify existing materialization; absence permits replay, drift never does."""
    root = locked.to_dependency_ref().get_install_path(caller_root / "apm_modules")
    ensure_path_within(root, caller_root / "apm_modules")
    if has_symlink_component(caller_root, root):
        raise ContractError("Installed source contains a symlink.", code="source_changed")
    if not root.exists():
        return None
    if not root.is_dir():
        raise ContractError("Installed source is not a directory.", code="source_changed")
    bounded_tree(root, limits)
    if not verify_package_hash(root, locked.content_hash):
        raise ContractError("Installed source differs from its lock.", code="source_changed")
    return root.resolve()


def _offline_source(
    requested: DependencyReference, caller_root: Path, limits: ContractLimits
) -> tuple[Path, str, str]:
    """Select a declared, exact installed root without invoking transport owners."""
    pin = _caller_source_pin(requested, caller_root, limits)
    root = _installed_source(pin[1], caller_root, limits) if pin else None
    if root is None:
        raise ContractError(
            "Remote source is unresolved offline. Execute with --allow-advisory to acquire it.",
            code="unresolved_source",
            outcome=Outcome.UNPROVEN,
        )
    locked = pin[1]
    return root, locked.resolved_commit, locked.content_hash


@contextmanager
def _private_root(caller_root: Path, original_root: Path | None) -> Iterator[Path]:
    """Allocate exclusive preparation outside the selected source, then remove it."""
    parent = caller_root
    if original_root is not None and parent.is_relative_to(original_root):
        parent = original_root.parent
    directory = parent / (".apmx-source-" + uuid4().hex)
    directory.mkdir(mode=0o700)
    try:
        yield directory
    finally:
        safe_rmtree(directory, parent)


def _copy_preparation(
    root: Path, destination: Path, contract: LeafContract, limits: ContractLimits
) -> tuple[bytes, bytes | None]:
    """Select only contract resources and declarations, never native activation."""
    expected = source_hash(root, limits)
    selected = {"apm.yml", contract.path.relative_to(root).as_posix()}
    selected.update(_check_names(root, limits))
    lock_path = resolve_lockfile_path_for_read(root, read_only=True)
    original_lock = None
    if lock_path.exists():
        selected.add(lock_path.name)
        original_lock = _read_bytes(lock_path, maximum=limits.file_bytes, root=root)
    manifest = _read_bytes(root / "apm.yml", maximum=limits.source_bytes, root=root)
    destination.mkdir(mode=0o700)
    for name in sorted(selected):
        raw, entry = _read(root, name, limits.file_bytes)
        _write(destination, entry, raw)
    if source_hash(root, limits) != expected:
        raise ContractError("Package changed during preparation capture.", code="source_changed")
    return manifest, original_lock


def _download(
    dependency: DependencyReference,
    target: Path,
    *,
    reference_text: str | None = None,
    materialize: bool = False,
    contract_path: str | None = None,
) -> tuple[DependencyReference, str, Path]:
    """Use the canonical host-boundary and authenticated package downloader."""
    from git.exc import GitError
    from requests import RequestException

    from apm_cli.config import get_apm_allow_protocol_fallback, get_apm_protocol_pref
    from apm_cli.deps.github_downloader import GitHubPackageDownloader
    from apm_cli.deps.transport_selection import ProtocolPreference
    from apm_cli.install.artifactory_resolver import _resolve_artifactory_boundary
    from apm_cli.install.gitlab_resolver import _try_resolve_gitlab_direct_shorthand
    from apm_cli.install.package_resolution import resolve_parsed_dependency_reference
    from apm_cli.utils.git_env import redact_git_diagnostic
    from apm_cli.utils.github_host import is_full_commit_sha

    downloader = GitHubPackageDownloader(
        protocol_pref=ProtocolPreference.from_str(get_apm_protocol_pref(create_config=False)),
        allow_fallback=get_apm_allow_protocol_fallback(create_config=False),
        contract_path=contract_path,
    )
    try:
        if reference_text is not None:
            dependency, _ = resolve_parsed_dependency_reference(
                reference_text,
                None,
                dependency_reference_cls=DependencyReference,
                try_resolve_gitlab_direct_shorthand=_try_resolve_gitlab_direct_shorthand,
                auth_resolver=downloader.auth_resolver,
                verbose=False,
                resolve_artifactory_boundary=_resolve_artifactory_boundary,
            )
        validate_reference(dependency)
        if materialize:
            target = dependency.get_install_path(target)
        info = downloader.download_package(dependency, target)
    except (ValueError, RuntimeError, GitError, RequestException) as exc:
        if isinstance(exc, ContractError):
            raise
        detail = redact_git_diagnostic(str(exc))
        detail = " ".join(detail.split())
        detail = "".join(character if " " <= character <= "~" else "?" for character in detail)[
            :800
        ]
        raise ContractError(
            f"Package acquisition failed: {detail}. "
            "Check the package contents, reference and configured Git access.",
            code="source_acquisition",
        ) from exc
    revision = info.resolved_reference.resolved_commit if info.resolved_reference else None
    if not is_full_commit_sha(revision):
        raise ContractError("Downloaded source lacks a resolved commit.", code="unresolved_source")
    return dependency, revision, target


def _expand_import(
    original_root: Path,
    prepared: Path,
    dependency: DependencyReference,
    parent: DependencyReference | None,
    modules: Path | None,
    limits: ContractLimits,
) -> DependencyReference:
    """Reuse the dependency resolver's same-repository expansion, without graph traversal."""
    if not dependency.is_parent_repo_inheritance and not (parent and dependency.is_local):
        return dependency
    if parent is None or modules is None:
        raise ContractError(
            "Git parent imports require a remote package.", code="unsupported_import"
        )
    package, data, _ = read_project_manifest(original_root, limits)
    package.source_path = APMDependencyResolver._compute_dep_source_path(
        parent, None, original_root
    )
    resolver = APMDependencyResolver(apm_modules_dir=modules)
    effective = (
        resolver.expand_parent_repo_decl(parent, dependency)
        if dependency.is_parent_repo_inheritance
        else resolver._expand_remote_parent_local_path(parent, package, dependency)
    )
    validate_reference(effective)
    for key in ("dependencies", "devDependencies"):
        if (data.get(key) or {}).get("apm"):
            data[key]["apm"] = [effective.to_apm_yml_entry()]
    dump_yaml(data, prepared / "apm.yml")
    return effective


def _materialize_skill(
    root: Path,
    original_root: Path,
    contract: LeafContract,
    dependency: DependencyReference,
    limits: ContractLimits,
    *,
    remote_parent: bool,
) -> None:
    """Materialize exactly one skill, recording one canonical direct lock entry."""
    package, _, _ = read_project_manifest(root, limits)
    locked = LockedDependency.from_dependency_ref(
        dependency,
        None,
        depth=1,
        resolved_by=None,
        is_dev=bool((package.dev_dependencies or {}).get("apm")),
    )
    existing = _read_lock(root, limits)
    previous = existing.get_dependency(dependency.get_unique_key()) if existing else None
    if existing is not None and previous is None:
        raise ContractError(
            "Existing lock cannot be mapped to the expanded direct import. "
            "Publish a matching direct Git declaration and lock.",
            code="import_drift",
        )
    target = locked.to_dependency_ref().get_install_path(root / "apm_modules")
    ensure_path_within(target, root / "apm_modules")
    if target.exists() or has_symlink_component(root, target):
        raise ContractError(
            "Partial import content exists. Repair the package explicitly.", code="import_drift"
        )
    if dependency.is_local:
        if remote_parent:
            raise ContractError(
                "Remote packages cannot acquire local-path dependencies.", code="unsupported_import"
            )
        from apm_cli.install.phases.local_content import _copy_local_package

        parent_package, _, _ = read_project_manifest(original_root, limits)
        local_path = Path(dependency.local_path or "").expanduser()
        candidate = local_path if local_path.is_absolute() else original_root / local_path
        if has_symlink_component(Path(candidate.anchor), candidate):
            raise ContractError("Local import path contains a symlink.", code="source_escape")
        original = APMDependencyResolver._compute_dep_source_path(
            dependency, parent_package, target
        )
        _self_contained(original, limits)
        expected = source_hash(original, limits)
        if previous and previous.content_hash and expected != previous.content_hash:
            raise ContractError("Local skill differs from its existing lock.", code="import_drift")
        copied = _copy_local_package(
            dependency,
            target,
            original_root,
            project_root=root,
            logger=InstallLogger(verbose=False),
        )
        if (
            copied is None
            or source_hash(original, limits) != expected
            or source_hash(target, limits) != expected
        ):
            raise ContractError("Local import changed during preparation.", code="import_drift")
    else:
        download_ref = dependency
        if previous is not None:
            if (
                detect_ref_change(dependency, previous)
                or not previous.resolved_commit
                or not previous.content_hash
            ):
                raise ContractError(
                    "Existing import lock cannot be reproduced.", code="import_drift"
                )
            download_ref = build_download_ref(
                dependency, existing, update_refs=False, ref_changed=False
            )
        _, revision, _ = _download(download_ref, target)
        locked.resolved_commit = revision
        if previous is not None:
            bounded_tree(target, limits)
            if revision != previous.resolved_commit or not verify_package_hash(
                target, previous.content_hash
            ):
                raise ContractError(
                    "Acquired skill differs from its existing lock.", code="import_drift"
                )
    _self_contained(target, limits)
    skill_package, _, _ = read_project_manifest(target, limits, allow_missing=True)
    if any((skill_package.dependencies or {}).values()) or any(
        (skill_package.dev_dependencies or {}).values()
    ):
        raise ContractError(
            "Transitive skill dependencies are unsupported.", code="unsupported_import"
        )
    locked.content_hash = source_hash(target, limits)
    lock = LockFile()
    lock.add_dependency(locked)
    lock.write(root / "apm.lock.yaml")
    package, _, _ = read_project_manifest(root, limits)
    resolve_installed_skills(contract, root, package, limits=limits)


def _missing_import(
    root: Path, contract: LeafContract, limits: ContractLimits, *, planning: bool
) -> DependencyReference | None:
    dependency = package_dependency(root, contract, limits)
    lock = _read_lock(root, limits)
    if lock is not None and (
        len(lock.dependencies) != (1 if dependency else 0)
        or (dependency and lock.get_dependency(dependency.get_unique_key()) is None)
        or lock.mcp_servers
        or lock.lsp_servers
    ):
        raise ContractError(
            "Package lock must describe exactly its direct skill.", code="unsupported_import"
        )
    if lock is not None and dependency is not None:
        locked = lock.get_dependency(dependency.get_unique_key())
        if locked is None or (
            locked.depth != 1
            or locked.resolved_by
            or locked.declaring_parent
            or detect_ref_change(dependency, locked)
            or locked.to_dependency_ref().get_identity() != dependency.get_identity()
        ):
            raise ContractError(
                "Existing import lock is not an exact direct dependency. Repair it explicitly.",
                code="import_drift",
            )
    package, _, _ = read_project_manifest(root, limits)
    try:
        resolve_installed_skills(contract, root, package, limits=limits)
    except ContractError as exc:
        if dependency is None or exc.code not in {"missing_lock", "missing_import"}:
            raise
        if planning:
            raise ContractError(
                "Imported skill is unresolved offline. Execute to prepare its direct dependency.",
                code="unresolved_import",
                outcome=Outcome.UNPROVEN,
            ) from exc
        return dependency
    return None


@contextmanager
def prepare_contract_source(
    package_ref: str,
    contract_relative_path: str,
    *,
    caller_root: Path,
    planning: bool,
    limits: ContractLimits,
) -> Iterator[ContractSource]:
    """Prepare one explicit package source; never activate or modify caller setup."""
    try:
        dependency = DependencyReference.parse(package_ref)
        validate_reference(dependency)
        _contract_path(caller_root, contract_relative_path)
        if dependency.is_local:
            raw = Path(dependency.local_path or "").expanduser()
            original = raw if raw.is_absolute() else caller_root / raw
            if has_symlink_component(
                (original.anchor and Path(original.anchor)) or caller_root, original
            ):
                raise ContractError("Local package path contains a symlink.", code="source_escape")
            root = original.resolve()
            digest = source_hash(root, limits)
            contract = parse_contract(_contract_path(root, contract_relative_path), limits=limits)
            missing = _missing_import(root, contract, limits, planning=planning)
            if missing is None:
                yield ContractSource(root, contract_relative_path, package_ref, package_hash=digest)
                return
            with _private_root(caller_root, root) as private:
                prepared = private / "package"
                manifest, lock = _copy_preparation(root, prepared, contract, limits)
                missing = _expand_import(root, prepared, missing, None, None, limits)
                _materialize_skill(prepared, root, contract, missing, limits, remote_parent=False)
                if source_hash(root, limits) != digest:
                    raise ContractError(
                        "Original package changed during preparation.", code="source_changed"
                    )
                yield ContractSource(
                    prepared,
                    contract_relative_path,
                    package_ref,
                    package_hash=digest,
                    prepared_hash=source_hash(prepared, limits),
                    original_root=root,
                    original_manifest=manifest,
                    original_lock=lock,
                )
            return
        if planning:
            root, revision, digest = _offline_source(dependency, caller_root, limits)
            contract = parse_contract(_contract_path(root, contract_relative_path), limits=limits)
            _missing_import(root, contract, limits, planning=True)
            yield ContractSource(
                root, contract_relative_path, package_ref, revision, digest, "locked-package-hash"
            )
            return
        pin = _caller_source_pin(dependency, caller_root, limits)
        installed = _installed_source(pin[1], caller_root, limits) if pin else None
        with _private_root(caller_root, None) as private:
            if installed is not None:
                modules = caller_root / "apm_modules"
                parent, revision, root = (
                    pin[1].to_dependency_ref(),
                    pin[1].resolved_commit,
                    installed,
                )
            else:
                modules = private / "packages"
                download_ref = build_download_ref(
                    dependency, pin[0] if pin else None, update_refs=False, ref_changed=False
                )
                parent, revision, root = _download(
                    download_ref,
                    modules,
                    reference_text=None if pin else package_ref,
                    materialize=True,
                    contract_path=contract_relative_path,
                )
            digest = source_hash(root, limits)
            if pin and (revision != pin[1].resolved_commit or digest != pin[1].content_hash):
                raise ContractError(
                    "Acquired source differs from the caller lock. "
                    "Check the locked revision and package integrity before retrying.",
                    code="source_changed",
                )
            contract = parse_contract(_contract_path(root, contract_relative_path), limits=limits)
            missing = _missing_import(root, contract, limits, planning=False)
            original_root = root
            manifest = lock = None
            if missing is not None:
                prepared = private / "prepared"
                manifest, lock = _copy_preparation(root, prepared, contract, limits)
                missing = _expand_import(
                    root, prepared, missing, replace(parent, reference=revision), modules, limits
                )
                _materialize_skill(prepared, root, contract, missing, limits, remote_parent=True)
                root = prepared
            if source_hash(original_root, limits) != digest:
                raise ContractError(
                    "Acquired package changed during preparation.", code="source_changed"
                )
            yield ContractSource(
                root,
                contract_relative_path,
                package_ref,
                revision,
                digest,
                "locked-package-hash" if pin else "observed-resolved-source",
                prepared_hash=source_hash(root, limits) if root != original_root else None,
                original_root=original_root,
                original_manifest=manifest,
                original_lock=lock,
            )
    except (ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError(
            "Invalid package source. Use a local APM directory or an explicit Git package reference.",
            code="invalid_source",
        ) from exc
