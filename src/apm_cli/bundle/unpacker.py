"""Bundle unpacker  -- extracts and verifies APM bundles."""

import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List  # noqa: F401, UP035

from ..deps.lockfile import LEGACY_LOCKFILE_NAME, LOCKFILE_NAME, LockFile


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
    pack_meta: dict = field(default_factory=dict)


def unpack_bundle(
    bundle_path: Path,
    output_dir: Path = Path("."),
    skip_verify: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> UnpackResult:
    """Extract and apply an APM bundle to a project directory.

    Additive-only semantics (v1): only writes files listed in the bundle's
    lockfile ``deployed_files``.  Never deletes existing files.  If a local
    file has the same name as a bundle file, the bundle file wins (overwrite).

    Args:
        bundle_path: Path to a ``.tar.gz`` archive or an unpacked bundle directory.
        output_dir: Target project directory to copy files into.
        skip_verify: If *True*, skip completeness verification against the lockfile.
        dry_run: If *True*, resolve the file list but write nothing to disk.
        force: If *True*, deploy even when critical hidden characters are found.

    Returns:
        :class:`UnpackResult` describing what was (or would be) extracted.

    Raises:
        FileNotFoundError: If the bundle's ``apm.lock.yaml`` is missing.
        ValueError: If verification finds files listed in the lockfile but
            absent from the bundle.
    """
    # 1. If archive, extract to temp dir
    cleanup_temp = False
    if bundle_path.is_file() and bundle_path.name.endswith(".tar.gz"):
        from ..config import get_apm_temp_dir

        temp_dir = Path(tempfile.mkdtemp(prefix="apm-unpack-", dir=get_apm_temp_dir()))
        cleanup_temp = True
        try:
            with tarfile.open(bundle_path, "r:gz") as tar:
                # Security: prevent path traversal and special entries
                for member in tar.getmembers():
                    if member.name.startswith("/") or ".." in member.name:
                        raise ValueError(f"Refusing to extract path-traversal entry: {member.name}")
                    if member.issym() or member.islnk():
                        raise ValueError(f"Refusing to extract symlink/hardlink: {member.name}")
                # filter="data" was added in Python 3.12; use it when available
                if sys.version_info >= (3, 12):
                    tar.extractall(temp_dir, filter="data")
                else:
                    tar.extractall(temp_dir)  # noqa: S202
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
                pack_meta=pack_meta,
            )

        # 4. Copy target files to output_dir (additive, no deletes)
        output_dir = Path(output_dir)
        output_dir_resolved = output_dir.resolve()
        skipped = 0
        for rel_path in unique_files:
            # Guard against absolute paths or path-traversal entries in deployed_files
            p = Path(rel_path)
            if p.is_absolute() or rel_path.startswith("/") or ".." in p.parts:
                raise ValueError(
                    f"Refusing to unpack unsafe path from bundle lockfile: {rel_path!r}"
                )
            dest = output_dir / rel_path
            if not dest.resolve().is_relative_to(output_dir_resolved):
                raise ValueError(
                    f"Refusing to unpack path that escapes output directory: {rel_path!r}"
                )
            src = source_dir / rel_path
            if src.is_symlink():
                # Security: skip symlinks to prevent scanning bypass
                skipped += 1
                continue
            if not src.exists():
                skipped += 1
                continue  # skip_verify may allow missing files
            if src.is_dir():
                from ..security.gate import ignore_symlinks

                shutil.copytree(src, dest, dirs_exist_ok=True, ignore=ignore_symlinks)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest, follow_symlinks=False)

        return UnpackResult(
            extracted_dir=bundle_path,
            files=unique_files,
            verified=verified,
            dependency_files=dep_file_map,
            skipped_count=skipped,
            security_warnings=security_warnings,
            security_critical=security_critical,
            pack_meta=pack_meta,
        )
    finally:
        # Clean up temp dir if we created one
        if cleanup_temp and temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
