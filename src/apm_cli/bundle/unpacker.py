"""Bundle unpacker  -- extracts and verifies APM bundles."""

import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..deps.lockfile import LEGACY_LOCKFILE_NAME, LOCKFILE_NAME, LockFile
from ..utils.archive import (
    MAX_ZIP_ENTRIES,
    MAX_ZIP_UNCOMPRESSED,
    ArchiveError,
    _extract_tar_gz_file,
    safe_extract_zip,
)

_MAX_ZIP_ENTRIES = MAX_ZIP_ENTRIES
_MAX_ZIP_UNCOMPRESSED = MAX_ZIP_UNCOMPRESSED


def _resolve_bundle_source(bundle_path: Path) -> tuple[Path, Path | None, bool]:
    """Extract an archive bundle to a temp dir, or return the dir as-is.

    Returns ``(source_dir, temp_dir, cleanup_temp)``.  The caller is
    responsible for ``shutil.rmtree(temp_dir)`` when ``cleanup_temp`` is True.
    """
    cleanup_temp = False
    if bundle_path.is_file() and bundle_path.name.endswith(".zip"):
        from ..config import get_apm_temp_dir

        temp_dir = Path(tempfile.mkdtemp(prefix="apm-unpack-", dir=get_apm_temp_dir()))
        cleanup_temp = True
        try:
            with zipfile.ZipFile(bundle_path, "r") as zf:
                safe_extract_zip(
                    zf,
                    temp_dir,
                    max_entries=_MAX_ZIP_ENTRIES,
                    max_uncompressed=_MAX_ZIP_UNCOMPRESSED,
                    error_type=ValueError,
                )
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        # Locate inner directory (the archive wraps a single top-level dir)
        children = list(temp_dir.iterdir())
        if len(children) == 1 and children[0].is_dir():  # noqa: SIM108
            source_dir = children[0]
        else:
            source_dir = temp_dir
    elif bundle_path.is_file() and bundle_path.name.endswith(".tar.gz"):
        # Legacy .tar.gz support (backward compat)
        from ..config import get_apm_temp_dir

        temp_dir = Path(tempfile.mkdtemp(prefix="apm-unpack-", dir=get_apm_temp_dir()))
        cleanup_temp = True
        try:
            _extract_tar_gz_file(bundle_path, str(temp_dir))
        except ArchiveError as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            msg = str(exc)
            if "path" in msg or "Symlinks" in msg or "links" in msg:
                raise ValueError(f"Refusing to extract path-traversal entry: {msg}") from exc
            raise ValueError(msg) from exc
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        # Locate inner directory (the archive wraps a single top-level dir)
        children = list(temp_dir.iterdir())
        if len(children) == 1 and children[0].is_dir():  # noqa: SIM108
            source_dir = children[0]
        else:
            source_dir = temp_dir
    elif bundle_path.is_dir():
        source_dir = bundle_path
        temp_dir = None
    else:
        raise FileNotFoundError(f"Bundle not found or unsupported format: {bundle_path}")
    return source_dir, temp_dir, cleanup_temp


def _copy_deployed_files(unique_files: list[str], source_dir: Path, output_dir: Path) -> int:
    """Copy bundle files to *output_dir* (additive, no deletes).

    Returns the count of skipped files (symlinks or already-missing sources).
    """
    output_dir = Path(output_dir)
    output_dir_resolved = output_dir.resolve()
    skipped = 0
    for rel_path in unique_files:
        # Guard against absolute paths or path-traversal entries in deployed_files
        p = Path(rel_path)
        if p.is_absolute() or rel_path.startswith("/") or ".." in p.parts:
            raise ValueError(f"Refusing to unpack unsafe path from bundle lockfile: {rel_path!r}")
        dest = output_dir / rel_path
        if not dest.resolve().is_relative_to(output_dir_resolved):
            raise ValueError(f"Refusing to unpack path that escapes output directory: {rel_path!r}")
        src = source_dir / rel_path
        if src.is_symlink():
            # Security: skip symlinks to prevent scanning bypass
            skipped += 1
            continue
        if not src.exists():
            skipped += 1
            continue  # skip_verify may allow missing files
        if src.is_dir():
            from ..security.gate import ignore_non_content

            shutil.copytree(src, dest, dirs_exist_ok=True, ignore=ignore_non_content)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest, follow_symlinks=False)
    return skipped


@dataclass
class UnpackResult:
    """Result of an unpack operation."""

    extracted_dir: Path
    files: list[str] = field(default_factory=list)
    verified: bool = False
    dependency_files: dict[str, list[str]] = field(default_factory=dict)
    skipped_count: int = 0
    security_warnings: int = 0
    security_critical: int = 0
    canvas_blocked: int = 0
    pack_meta: dict = field(default_factory=dict)


def unpack_bundle(
    bundle_path: Path,
    output_dir: Path = Path("."),
    skip_verify: bool = False,
    dry_run: bool = False,
    force: bool = False,
    trust_canvas: bool = False,
) -> UnpackResult:
    """Extract and apply an APM bundle to a project directory.

    Additive-only semantics (v1): only writes files listed in the bundle's
    lockfile ``deployed_files``.  Never deletes existing files.  If a local
    file has the same name as a bundle file, the bundle file wins (overwrite).

    Args:
        bundle_path: Path to a ``.zip`` (or legacy ``.tar.gz``) archive, or an unpacked bundle directory.
        output_dir: Target project directory to copy files into.
        skip_verify: If *True*, skip completeness verification against the lockfile.
        dry_run: If *True*, resolve the file list but write nothing to disk.
        force: If *True*, deploy even when critical hidden characters are found.
        trust_canvas: If *True*, allow executable canvas extension files
            (``.github/extensions/<name>/extension.mjs``) to be unpacked.
            Defaults to *False* (fail closed) so a vendored bundle cannot
            smuggle executable canvas code past the trust gate.

    Returns:
        :class:`UnpackResult` describing what was (or would be) extracted.

    Raises:
        FileNotFoundError: If the bundle's ``apm.lock.yaml`` is missing.
        ValueError: If verification finds files listed in the lockfile but
            absent from the bundle.
    """
    # 1. If archive, extract to temp dir
    source_dir, temp_dir, cleanup_temp = _resolve_bundle_source(bundle_path)

    try:
        # 2. Read apm.lock.yaml (or legacy apm.lock) from bundle
        lockfile_path = source_dir / LOCKFILE_NAME
        if not lockfile_path.exists():
            # Backward compat: older bundles used "apm.lock"
            legacy_lockfile_path = source_dir / LEGACY_LOCKFILE_NAME
            if legacy_lockfile_path.exists():
                lockfile_path = legacy_lockfile_path

        # Extract pack: metadata (written by apm pack) before structured parse
        pack_meta: dict = {}
        try:
            import yaml

            raw = yaml.safe_load(lockfile_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                val = raw.get("pack", {})
                pack_meta = val if isinstance(val, dict) else {}
        except Exception:
            pass  # non-critical -- proceed without metadata

        lockfile = LockFile.read(lockfile_path)
        if lockfile is None:
            if not lockfile_path.exists():
                raise FileNotFoundError(
                    f"{lockfile_path.name} not found in the bundle  -- the bundle may be incomplete."
                )
            raise FileNotFoundError(
                f"{lockfile_path.name} in the bundle could not be parsed  -- the bundle may be corrupt."
            )

        # Collect deployed_files per dependency and deduplicated global list
        dep_file_map: dict[str, list[str]] = {}
        seen: set[str] = set()
        unique_files: list[str] = []
        for dep in lockfile.get_all_dependencies():
            dep_key = dep.get_unique_key()
            dep_files: list[str] = []
            for f in dep.deployed_files:
                dep_files.append(f)
                if f not in seen:
                    seen.add(f)
                    unique_files.append(f)
            if dep_files:
                dep_file_map[dep_key] = dep_files

        # Security + feature gate: canvas extensions are executable Node
        # bundles (``extension.mjs``).  ``apm unpack`` copies deployed files
        # verbatim WITHOUT routing through ``CanvasIntegrator``, so neither
        # the experimental feature flag nor its trust gate would otherwise
        # apply.  Require BOTH gates -- the ``canvas`` experimental flag ON
        # (feature availability) AND ``trust_canvas`` (executable-code trust)
        # -- before unpacking canvas paths.  Fail closed: drop them when
        # either gate is missing.
        canvas_blocked = 0
        from ..core.experimental import is_enabled
        from ..integration.canvas_integrator import is_canvas_bundle_path

        if not (is_enabled("canvas") and trust_canvas):
            _blocked = {f for f in unique_files if is_canvas_bundle_path(f)}
            if _blocked:
                canvas_blocked = len(_blocked)
                unique_files = [f for f in unique_files if f not in _blocked]
                for _k in list(dep_file_map):
                    dep_file_map[_k] = [f for f in dep_file_map[_k] if f not in _blocked]
                    if not dep_file_map[_k]:
                        del dep_file_map[_k]

        # 3. Verify completeness
        verified = True
        if not skip_verify:
            missing = [f for f in unique_files if not (source_dir / f).exists()]
            if missing:
                raise ValueError(
                    "Bundle verification failed  -- the following deployed files "
                    "are missing from the bundle:\n" + "\n".join(f"  - {m}" for m in missing)
                )

        if skip_verify:
            verified = False

        # 3b. Security scan: check bundle contents for hidden Unicode characters
        from ..security.gate import BLOCK_POLICY, SecurityGate

        # Scan all files under source_dir (SecurityGate handles symlink
        # skipping, directory recursion, and OSError resilience)
        verdict = SecurityGate.scan_files(source_dir, policy=BLOCK_POLICY, force=force)
        security_warnings = verdict.warning_count
        security_critical = verdict.critical_count

        if verdict.should_block:
            affected = []
            for path, findings in verdict.findings_by_file.items():
                c = sum(1 for f in findings if f.severity == "critical")
                if c > 0:
                    affected.append(f"  {path}  ({c} critical)")
            raise ValueError(
                f"Blocked: bundle contains {len(affected)} file(s) "
                f"with critical hidden characters\n\n"
                f"Affected files:\n" + "\n".join(affected) + "\n\n"
                "Next steps:\n"
                "  - Extract the bundle and run: apm audit --file <path> to inspect\n"
                "  - Run: apm unpack --force to deploy anyway "
                "(not recommended)\n\n"
                "Learn more: https://apm.github.io/apm/enterprise/security/"
            )

        # Dry-run: return file list without writing
        if dry_run:
            return UnpackResult(
                extracted_dir=bundle_path,
                files=unique_files,
                verified=verified,
                dependency_files=dep_file_map,
                security_warnings=security_warnings,
                security_critical=security_critical,
                canvas_blocked=canvas_blocked,
                pack_meta=pack_meta,
            )

        # 4. Copy target files to output_dir (additive, no deletes)
        skipped = _copy_deployed_files(unique_files, source_dir, output_dir)

        return UnpackResult(
            extracted_dir=bundle_path,
            files=unique_files,
            verified=verified,
            dependency_files=dep_file_map,
            skipped_count=skipped,
            security_warnings=security_warnings,
            security_critical=security_critical,
            canvas_blocked=canvas_blocked,
            pack_meta=pack_meta,
        )
    finally:
        # Clean up temp dir if we created one
        if cleanup_temp and temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
