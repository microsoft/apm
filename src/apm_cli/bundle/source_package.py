"""Non-activating source-package envelope over existing APM bundle integrity."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..agent_plugins.assets import AssetInventory
from ..agent_plugins.ir import AgentPluginAsset
from ..deps.lockfile import LOCKFILE_NAME, LockFile, require_supported_lockfile_version
from ..models.apm_package import APMPackage
from ..models.package_resources import collect_package_resources
from ..utils.archive import projected_archive_path, write_tar_archive, write_zip_archive
from ..utils.atomic_io import write_text_lf
from ..utils.path_security import (
    ensure_path_within,
    has_symlink_component,
    source_permission_bits,
    validate_portable_relative_path,
)
from ..utils.yaml_io import load_yaml_str
from .formats import BundleFormat
from .lockfile_enrichment import enrich_lockfile_for_pack

if TYPE_CHECKING:
    from .packer import PackResult
    from .unpacker import UnpackResult

_PAYLOAD = "package"
_METADATA_BYTES = 4 * 1024 * 1024


def _read_mapping(inventory: AssetInventory, path: Path) -> tuple[dict[str, Any], bytes]:
    _, payload = inventory.read_file(path, max_bytes=_METADATA_BYTES)
    try:
        data = load_yaml_str(payload.decode("utf-8"), reject_duplicate_keys=True)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid source-package metadata {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Source-package metadata {path.name} must be a mapping")
    return data, payload


def _source_inventory(
    root: Path,
) -> tuple[APMPackage, AssetInventory, tuple[AgentPluginAsset, ...]]:
    inventory = AssetInventory(root)
    names = {entry.name for entry in inventory.list_component_candidates(root)}
    if not {"apm.yml", LOCKFILE_NAME} <= names:
        raise ValueError("Source packages require exact apm.yml and apm.lock.yaml metadata names")
    manifest, _ = _read_mapping(inventory, root / "apm.yml")
    package = APMPackage.from_mapping(manifest, package_path=root, create_config=False)
    if not package.resources:
        raise ValueError("Source packages require explicit 'resources:' directory roots in apm.yml")
    lock_data, lock_bytes = _read_mapping(inventory, root / LOCKFILE_NAME)
    require_supported_lockfile_version(lock_data)
    LockFile.from_yaml(lock_bytes.decode("utf-8"))
    assets = (
        inventory.collect_file(root / "apm.yml"),
        inventory.collect_file(root / LOCKFILE_NAME),
        *collect_package_resources(root, package.resources, inventory),
    )
    for asset in assets:
        source_permission_bits(asset.source.path.lstat().st_mode)
        if has_symlink_component(root, root / asset.path, raise_on_error=True):
            raise ValueError(f"Source-package symlink rejected: {asset.path}")
    return package, inventory, assets


def require_resource_pack_mode(package: APMPackage, *, source: bool = False) -> None:
    """Refuse silent resource loss in every deployment/plugin exporter."""
    if package.resources and not source:
        raise ValueError(
            "Declared resources are non-activating package content, not plugin primitives. "
            "Use 'apm pack --format apm --source' to preserve them."
        )


def reject_source_deployment(root: Path) -> None:
    """Block a marked source envelope before any deployable-format admission."""
    root = root.resolve()
    path = root / LOCKFILE_NAME
    if not path.exists() and not path.is_symlink():
        return
    data, _ = _read_mapping(AssetInventory(root), path)
    pack = data.get("pack")
    if isinstance(pack, dict) and "source" in pack:
        raise ValueError(
            "Source packages cannot be deployed or installed. "
            "Use 'apm unpack --source <bundle> -o <new-directory>' to restore inert bytes."
        )


def _require_new_destination(destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise ValueError(
            f"Source-package output already exists: {destination}. Choose a new directory or archive."
        )
    if has_symlink_component(destination.parent, destination, raise_on_error=True):
        raise ValueError(f"Source-package output is symlinked: {destination}")


def _copy_assets(
    inventory: AssetInventory, assets: tuple[AgentPluginAsset, ...], destination: Path
) -> None:
    from .local_bundle import verify_bundle_integrity

    for asset in assets:
        target = destination / asset.path
        ensure_path_within(target, destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with inventory.open_verified_asset(asset) as source, target.open("xb") as output:
            mode = source_permission_bits(os.fstat(source.fileno()).st_mode)
            remaining = asset.size
            while remaining:
                chunk = source.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError(f"Source-package file changed while copying: {asset.path}")
                output.write(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise ValueError(f"Source-package file grew while copying: {asset.path}")
        target.chmod(mode)
    errors = verify_bundle_integrity(
        destination, {"pack": {"bundle_files": {asset.path: asset.sha256 for asset in assets}}}
    )
    if errors:
        raise ValueError("Source-package copy failed integrity verification: " + "; ".join(errors))


def pack_source_package(
    project_root: Path,
    output_dir: Path,
    *,
    archive: bool,
    archive_format: str,
    dry_run: bool,
) -> PackResult:
    """Pack exact author metadata and selected resources, with no deployment writes."""
    from .packer import PackResult
    from .plugin_exporter import _sanitize_bundle_name

    if project_root.is_symlink():
        raise ValueError("Source-package root must not be a symlink")
    root = project_root.resolve()
    package, inventory, assets = _source_inventory(root)
    name = _sanitize_bundle_name(f"{package.name}-{package.version}")
    validate_portable_relative_path(name, context="source bundle name")
    destination = (
        projected_archive_path(output_dir, name, archive_format) if archive else output_dir / name
    )
    _require_new_destination(destination)
    for resource in package.resources:
        if destination.resolve().is_relative_to((root / resource).resolve()):
            raise ValueError("Source-package output cannot be inside a selected resource directory")
    result = PackResult(
        bundle_path=destination,
        files=[asset.path for asset in assets],
        lockfile_enriched=True,
    )
    if dry_run:
        return result
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".apm-source-", dir=output_dir) as temporary:
        staged = Path(temporary) / name
        staged.mkdir()
        _copy_assets(inventory, assets, staged / _PAYLOAD)
        hashes = {f"{_PAYLOAD}/{asset.path}": asset.sha256 for asset in assets}
        write_text_lf(
            staged / LOCKFILE_NAME,
            enrich_lockfile_for_pack(
                LockFile(), BundleFormat.APM, "all", bundle_files=hashes, source=True
            ),
        )
        # Revalidate staged bytes before publishing either representation.
        _verify_source_package(staged)
        _require_new_destination(destination)
        if archive:
            staged_archive = projected_archive_path(Path(temporary), name, archive_format)
            writer = write_tar_archive if archive_format == "tar.gz" else write_zip_archive
            writer(staged, staged_archive)
            os.link(staged_archive, destination)
        else:
            staged.rename(destination)
    return result


def _verify_source_package(
    root: Path,
) -> tuple[dict[str, Any], AssetInventory, tuple[AgentPluginAsset, ...]]:
    from .local_bundle import verify_bundle_integrity

    if root.is_symlink():
        raise ValueError("Source-package root must not be a symlink")
    root = root.resolve()
    envelope_inventory = AssetInventory(root)
    envelope_assets = envelope_inventory.collect_component(root)
    for asset in envelope_assets:
        source_permission_bits(asset.source.path.lstat().st_mode)
        validate_portable_relative_path(asset.path, context="source bundle path")
        if has_symlink_component(root, root / asset.path, raise_on_error=True):
            raise ValueError(f"Source-package symlink rejected: {asset.path}")
    metadata, raw = _read_mapping(envelope_inventory, root / LOCKFILE_NAME)
    require_supported_lockfile_version(metadata)
    LockFile.from_yaml(raw.decode("utf-8"))
    pack = metadata.get("pack")
    if (
        not isinstance(pack, dict)
        or pack.get("source") is not True
        or pack.get("format") != BundleFormat.APM.value
        or not isinstance(pack.get("bundle_files"), dict)
        or not pack["bundle_files"]
    ):
        raise ValueError("Not an attested APM source package; repack with --format apm --source")
    files = pack["bundle_files"]
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in files.items()):
        raise ValueError("Source-package bundle_files must map relative paths to SHA-256 strings")
    actual = {asset.path for asset in envelope_assets} - {LOCKFILE_NAME}
    if set(files) != actual:
        raise ValueError("Source-package inventory is missing, unlisted, or ambiguous")
    errors = verify_bundle_integrity(root, metadata)
    if errors:
        raise ValueError("Source-package integrity verification failed: " + "; ".join(errors))
    _, inventory, assets = _source_inventory(root / _PAYLOAD)
    if set(files) != {f"{_PAYLOAD}/{asset.path}" for asset in assets}:
        raise ValueError("Source-package inventory does not match declared resources and metadata")
    return pack, inventory, assets


def restore_source_package(root: Path, output: Path, *, dry_run: bool) -> UnpackResult:
    """Restore an attested payload into a new directory without executing anything."""
    from .unpacker import UnpackResult

    _require_new_destination(output)
    pack, inventory, assets = _verify_source_package(root)
    result = UnpackResult(
        extracted_dir=output,
        files=[asset.path for asset in assets],
        verified=True,
        pack_meta=pack,
    )
    if dry_run:
        return result
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".apm-restore-", dir=output.parent) as temporary:
        staged = Path(temporary) / "payload"
        staged.mkdir()
        _copy_assets(inventory, assets, staged)
        _require_new_destination(output)
        staged.rename(output)
    return result
