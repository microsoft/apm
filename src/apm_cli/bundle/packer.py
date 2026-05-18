"""Bundle packer  -- creates self-contained APM bundles from the resolved dependency tree."""

from __future__ import annotations

import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from ..core.target_detection import detect_target
from ..deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed
from ..security.gate import ignore_non_content
from .lockfile_enrichment import _filter_files_by_target, enrich_lockfile_for_pack
from .packer_helpers import (
    collect_deployed_files,
    copy_bundle_files,
    scan_bundle_security,
    validate_package_metadata,
    verify_file_safety_and_existence,
)

_COPYTREE_IGNORE = ignore_non_content


@dataclass
class PackResult:
    """Result of a pack operation."""

    bundle_path: Path
    files: list[str] = field(default_factory=list)
    lockfile_enriched: bool = False
    mapped_count: int = 0
    path_mappings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PackOptions:
    """Options controlling the pack operation."""

    fmt: str = "apm"
    target: str | list[str] | None = None
    archive: bool = False
    dry_run: bool = False
    force: bool = False


def _resolve_pack_options(options: PackOptions | None, legacy_kwargs: dict) -> PackOptions:
    """Normalise ``options`` from legacy keyword callers.

    Extracted so ``pack_bundle`` doesn't pay the C901 complexity penalty for
    the backward-compat shim.
    """
    if legacy_kwargs and options is None:
        _valid = PackOptions.__dataclass_fields__
        options = PackOptions(**{k: v for k, v in legacy_kwargs.items() if k in _valid})
    return options or PackOptions()


def pack_bundle(
    project_root: Path,
    output_dir: Path,
    options: PackOptions | None = None,
    logger=None,
    **legacy_kwargs,
) -> PackResult:
    """Create a self-contained bundle from installed APM dependencies.

    Args:
        project_root: Root of the project containing ``apm.lock.yaml`` and ``apm.yml``.
        output_dir: Directory where the bundle will be created.
        options: Pack options (fmt, target, archive, dry_run, force).
        logger: Optional logger for progress and warnings.
        **legacy_kwargs: Deprecated -- pass individual fields through ``PackOptions`` instead.

    Returns:
        :class:`PackResult` describing what was (or would be) produced.

    Raises:
        FileNotFoundError: If ``apm.lock.yaml`` is missing.
        ValueError: If deployed files referenced in the lockfile are missing on disk.
    """
    opts = _resolve_pack_options(options, legacy_kwargs)

    # 1. Read lockfile (migrate legacy apm.lock -> apm.lock.yaml if needed)
    migrate_lockfile_if_needed(project_root)

    # Plugin format: delegate to dedicated exporter
    if opts.fmt == "plugin":
        from .plugin_exporter import ExportOptions, export_plugin_bundle

        return export_plugin_bundle(
            project_root=project_root,
            output_dir=output_dir,
            options=ExportOptions(
                target=opts.target,
                archive=opts.archive,
                dry_run=opts.dry_run,
                force=opts.force,
            ),
            logger=logger,
        )

    lockfile_path = get_lockfile_path(project_root)
    lockfile = LockFile.read(lockfile_path)
    if lockfile is None:
        raise FileNotFoundError(
            "apm.lock.yaml not found  -- run 'apm install' first to resolve dependencies."
        )

    # 2. Read apm.yml for name / version / config target
    apm_yml_path = project_root / "apm.yml"
    skill_md_path = project_root / "SKILL.md"
    pkg_name, pkg_version, config_target = validate_package_metadata(
        project_root, apm_yml_path, skill_md_path, logger
    )

    # 3. Resolve effective target
    if isinstance(opts.target, list):
        # List from CLI (e.g. --target claude,copilot) passes through directly
        effective_target = opts.target
    elif isinstance(config_target, list) and opts.target is None:
        # List from apm.yml target: [claude, copilot]
        effective_target = config_target
    else:
        effective_target, _reason = detect_target(
            project_root,
            explicit_target=opts.target,
            config_target=config_target if isinstance(config_target, str) else None,
        )
        # For packing purposes, "minimal" means nothing to pack  -- treat as "all"
        if effective_target == "minimal":
            effective_target = "all"

    # 4. Collect deployed_files from all dependencies, filtered by target.
    #    Skip local-source entries: these include the synthesised root self-entry
    #    (local_path == ".") and any local-path manifest deps. Local content is
    #    not portable and is bundled separately via the project's own files
    #    (or rejected outright for manifest-declared local deps).
    all_deployed = collect_deployed_files(lockfile)

    filtered_files, path_mappings = _filter_files_by_target(all_deployed, effective_target)
    # Deduplicate while preserving order
    seen = set()
    unique_files: list[str] = []
    for f in filtered_files:
        if f not in seen:
            seen.add(f)
            unique_files.append(f)

    # 5. Verify each path is safe (no traversal) and exists on disk
    verify_file_safety_and_existence(unique_files, path_mappings, project_root)

    # Dry-run: return file list without writing anything
    if opts.dry_run:
        bundle_dir = output_dir / f"{pkg_name}-{pkg_version}"
        return PackResult(
            bundle_path=bundle_dir,
            files=unique_files,
            lockfile_enriched=True,
            mapped_count=len(path_mappings),
            path_mappings=path_mappings,
        )

    # 5b. Scan files for hidden characters before bundling.
    # Intentionally non-blocking (warn only) -- pack is an authoring tool.
    # Critical findings here mean the author's own source files contain
    # hidden characters. We surface them so the author can fix before
    # publishing, but don't block the bundle. Consumers are protected by
    # install/unpack which block on critical.
    scan_bundle_security(unique_files, path_mappings, project_root, logger)

    # 6. Build output directory
    bundle_dir = output_dir / f"{pkg_name}-{pkg_version}"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # 7. Copy files preserving directory structure
    copy_bundle_files(unique_files, path_mappings, project_root, bundle_dir)

    # 8. Enrich lockfile copy and write to bundle
    enriched_yaml = enrich_lockfile_for_pack(lockfile, opts.fmt, effective_target)
    (bundle_dir / "apm.lock.yaml").write_text(enriched_yaml, encoding="utf-8")

    result = PackResult(
        bundle_path=bundle_dir,
        files=unique_files,
        lockfile_enriched=True,
        mapped_count=len(path_mappings),
        path_mappings=path_mappings,
    )

    # 10. Archive if requested
    if opts.archive:
        archive_path = output_dir / f"{pkg_name}-{pkg_version}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(bundle_dir, arcname=bundle_dir.name)
        shutil.rmtree(bundle_dir)
        result.bundle_path = archive_path

    return result
