"""Bundle packer -- creates self-contained APM bundles from the resolved dependency tree."""

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..core.target_detection import (
    detect_target,  # noqa: F401 -- RULE B: tests patch packer.detect_target
)
from ..deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed
from ..utils.archive import (
    projected_archive_path,
    validate_archive_format,
    write_tar_archive,
    write_zip_archive,
)
from ._packer_ops import (
    _collect_and_filter_deployed,
    _copy_files_verified,
    _resolve_package_meta,
    _scan_and_warn_hidden_chars,
    _verify_deployed_paths,
)
from .lockfile_enrichment import _filter_files_by_target, enrich_lockfile_for_pack  # noqa: F401


@dataclass
class PackResult:
    """Result of a pack operation."""

    bundle_path: Path
    files: list[str] = field(default_factory=list)
    lockfile_enriched: bool = False
    mapped_count: int = 0
    path_mappings: dict[str, str] = field(default_factory=dict)


def pack_bundle(
    project_root: Path,
    output_dir: Path,
    fmt: str = "apm",
    target: str | list[str] | None = None,
    archive: bool = False,
    archive_format: str = "zip",
    dry_run: bool = False,
    force: bool = False,
    logger=None,
) -> PackResult:
    """Create a self-contained bundle from installed APM dependencies.

    Args:
        project_root: Root of the project containing ``apm.lock.yaml`` and ``apm.yml``.
        output_dir: Directory where the bundle will be created.
        fmt: Bundle format -- ``"plugin"`` (Claude Code plugin layout) or ``"apm"`` (legacy).
        target: Target filter or *None* (auto-detect from apm.yml / project structure).
        archive: If *True*, produce a ``.zip`` / ``.tar.gz`` and remove the directory.
        archive_format: ``"zip"`` (default) or ``"tar.gz"``.
        dry_run: If *True*, resolve the file list but write nothing to disk.
        force: On collision (plugin format), last writer wins.

    Returns:
        :class:`PackResult` describing what was (or would be) produced.

    Raises:
        FileNotFoundError: If ``apm.lock.yaml`` is missing.
        ValueError: If deployed files referenced in the lockfile are missing on disk.
    """
    migrate_lockfile_if_needed(project_root)

    if fmt == "plugin":
        from .plugin_exporter import export_plugin_bundle

        return export_plugin_bundle(
            project_root=project_root,
            output_dir=output_dir,
            target=target,
            archive=archive,
            archive_format=archive_format,
            dry_run=dry_run,
            force=force,
            logger=logger,
        )

    lockfile_path = get_lockfile_path(project_root)
    lockfile = LockFile.read(lockfile_path)
    if lockfile is None:
        raise FileNotFoundError(
            "apm.lock.yaml not found -- run 'apm install' first to resolve dependencies."
        )

    pkg_name, pkg_version, effective_target = _resolve_package_meta(project_root, target, logger)

    unique_files, path_mappings, deployed_hashes, hash_dep_labels = _collect_and_filter_deployed(
        lockfile, effective_target
    )

    project_root_resolved = project_root.resolve()
    _verify_deployed_paths(unique_files, path_mappings, project_root, project_root_resolved)

    if dry_run:
        bundle_name = f"{pkg_name}-{pkg_version}"
        bundle_path = (
            projected_archive_path(output_dir, bundle_name, archive_format)
            if archive
            else output_dir / bundle_name
        )
        return PackResult(
            bundle_path=bundle_path,
            files=unique_files,
            lockfile_enriched=True,
            mapped_count=len(path_mappings),
            path_mappings=path_mappings,
        )

    _scan_and_warn_hidden_chars(unique_files, path_mappings, project_root, logger)

    bundle_dir = output_dir / f"{pkg_name}-{pkg_version}"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir_resolved = bundle_dir.resolve()

    _copy_files_verified(
        unique_files,
        path_mappings,
        deployed_hashes,
        hash_dep_labels,
        project_root,
        project_root_resolved,
        bundle_dir,
        bundle_dir_resolved,
    )

    enriched_yaml = enrich_lockfile_for_pack(lockfile, fmt, effective_target)
    (bundle_dir / "apm.lock.yaml").write_text(enriched_yaml, encoding="utf-8")

    result = PackResult(
        bundle_path=bundle_dir,
        files=unique_files,
        lockfile_enriched=True,
        mapped_count=len(path_mappings),
        path_mappings=path_mappings,
    )

    if archive:
        validate_archive_format(archive_format)
        archive_path = projected_archive_path(
            output_dir, f"{pkg_name}-{pkg_version}", archive_format
        )
        if archive_format == "tar.gz":
            write_tar_archive(bundle_dir, archive_path)
        else:
            write_zip_archive(bundle_dir, archive_path)
        shutil.rmtree(bundle_dir)
        result.bundle_path = archive_path

    return result
