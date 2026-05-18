"""Bundle write helpers extracted from exporter.py."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

from ...utils.path_security import PathTraversalError, ensure_path_within
from .utils import validate_output_rel

if TYPE_CHECKING:
    from ...deps.lockfile import LockFile
    from .exporter import BundleMetadata


def _write_bundle_files(
    file_map: dict[str, tuple[Path, str]],
    bundle_dir: Path,
) -> None:
    """Write collected files to bundle directory."""
    for output_rel, (source_abs, _owner) in file_map.items():
        if not validate_output_rel(output_rel):
            continue
        dest = bundle_dir / output_rel
        if source_abs.is_symlink():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            ensure_path_within(dest, bundle_dir)
        except PathTraversalError:
            continue
        shutil.copy2(source_abs, dest, follow_symlinks=False)


def _write_metadata_files(
    bundle_dir: Path,
    meta: BundleMetadata,
    logger,
) -> None:
    """Write hooks.json, .mcp.json, and plugin.json to bundle."""
    from .exporter import _update_plugin_json_paths

    if meta.merged_hooks:
        (bundle_dir / "hooks.json").write_text(
            json.dumps(meta.merged_hooks, indent=2, sort_keys=True), encoding="utf-8"
        )

    if meta.merged_mcp:
        (bundle_dir / ".mcp.json").write_text(
            json.dumps({"mcpServers": meta.merged_mcp}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    plugin_json = _update_plugin_json_paths(meta.plugin_json, meta.output_files, logger=logger)
    (bundle_dir / "plugin.json").write_text(
        json.dumps(plugin_json, indent=2, sort_keys=False), encoding="utf-8"
    )


def _write_enriched_lockfile(
    lockfile: LockFile | None,
    bundle_dir: Path,
    target: str | None,
) -> None:
    """Write enriched lockfile with bundle_files manifest."""
    if lockfile is None:
        return

    from ..lockfile_enrichment import enrich_lockfile_for_pack

    bundle_files: dict[str, str] = {}
    for fp in bundle_dir.rglob("*"):
        if not fp.is_file() or fp.is_symlink():
            continue
        rel = fp.relative_to(bundle_dir).as_posix()
        if rel == "apm.lock.yaml":
            continue
        bundle_files[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()

    # Issue #1207 D1: do NOT silently substitute ``"copilot"`` when
    # ``target`` is missing.  Bundles are target-agnostic at install
    # time; ``pack.target`` is recorded as informational metadata only.
    # Falling back to ``"all"`` preserves the lockfile-filter shape
    # (which uses target prefixes to narrow each dep's deployed_files
    # list to the union of supported targets) without locking the
    # bundle to a single client.
    enriched_yaml = enrich_lockfile_for_pack(
        lockfile,
        "plugin",
        target or "all",
        bundle_files=bundle_files,
    )
    (bundle_dir / "apm.lock.yaml").write_text(enriched_yaml, encoding="utf-8")


def _archive_bundle(bundle_dir: Path, output_dir: Path) -> Path:
    """Pack *bundle_dir* into a ``.tar.gz`` archive and remove the directory.

    Returns the path to the created archive.
    """
    archive_path = output_dir / f"{bundle_dir.name}.tar.gz"
    ensure_path_within(archive_path, output_dir)
    with tarfile.open(archive_path, "w:gz") as tar:

        def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            if info.issym() or info.islnk():
                return None  # reject symlinks injected after write
            return info

        tar.add(bundle_dir, arcname=bundle_dir.name, filter=_tar_filter)
    shutil.rmtree(bundle_dir)
    return archive_path
