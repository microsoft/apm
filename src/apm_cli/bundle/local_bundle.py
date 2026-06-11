"""Local-bundle detection, integrity verification, and target-mismatch checks.

This module powers ``apm install <local-bundle-path>`` (issue #1098).  A local
bundle is a directory, ``.zip``, or ``.tar.gz`` produced by ``apm pack`` -- it contains a
``plugin.json`` at its root and (for bundles produced by recent versions of
APM) an ``apm.lock.yaml`` carrying the per-file SHA-256 manifest under
``pack.bundle_files``.

Public surface:

- :class:`LocalBundleInfo` -- frozen descriptor returned by detection.
- :func:`detect_local_bundle` -- probe a path; return ``LocalBundleInfo`` or
  ``None``.  Tarballs are transparently extracted to a temp directory and the
  caller is responsible for cleanup via ``info.temp_dir``.
- :func:`verify_bundle_integrity` -- walk the bundle, hash every file listed
  under ``pack.bundle_files``, and return a list of error strings (empty
  means OK).  Symlinks are always rejected.
- :func:`check_target_mismatch` -- compare bundle targets to install
  targets; return a warning string when the bundle was packed for targets
  the caller is not installing into.
- :func:`read_bundle_plugin_json` -- parse ``plugin.json`` at bundle root;
  return ``{}`` when missing or invalid.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..utils.archive import (
    MAX_ZIP_ENTRIES,
    MAX_ZIP_UNCOMPRESSED,
    ArchiveError,
    _extract_tar_gz_file,
    safe_extract_zip,
)
from ..utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)

_MAX_ZIP_ENTRIES = MAX_ZIP_ENTRIES
_MAX_ZIP_UNCOMPRESSED = MAX_ZIP_UNCOMPRESSED


@dataclass(frozen=True)
class LocalBundleInfo:
    """Descriptor for a detected local bundle.

    Attributes:
        source_dir: Filesystem path to the bundle root.  For tarballs this
            points inside the extraction directory.
        plugin_json: Parsed ``plugin.json`` (empty dict when absent).
        package_id: Slug derived from ``plugin.json["id"]``, falling back to
            the bundle directory name.
        lockfile: Parsed ``apm.lock.yaml`` content (or ``None`` when the
            bundle was produced by an older APM version that did not embed
            the lockfile).
        pack_targets: Targets the bundle was packed for, derived from
            ``lockfile["pack"]["target"]``.  Empty list when unknown.
        is_archive: ``True`` when the source path was a ``.zip`` or ``.tar.gz``.
        temp_dir: Extraction directory for tarballs (caller must clean up).
            ``None`` for directory bundles.
    """

    source_dir: Path
    plugin_json: dict[str, Any]
    package_id: str
    lockfile: dict[str, Any] | None
    pack_targets: list[str] = field(default_factory=list)
    is_archive: bool = False
    temp_dir: Path | None = None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def read_bundle_plugin_json(bundle_dir: Path) -> dict[str, Any]:
    """Parse ``plugin.json`` at *bundle_dir*; return ``{}`` if missing."""
    pj_path = bundle_dir / "plugin.json"
    if not pj_path.is_file():
        return {}
    try:
        data = json.loads(pj_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_bundle_lockfile(bundle_dir: Path) -> dict[str, Any] | None:
    """Parse ``apm.lock.yaml`` at *bundle_dir*; return ``None`` if missing."""
    lf_path = bundle_dir / "apm.lock.yaml"
    if not lf_path.is_file():
        return None
    try:
        data = yaml.safe_load(lf_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _extract_pack_targets(lockfile: dict[str, Any] | None) -> list[str]:
    """Return list of pack targets from a parsed bundle lockfile."""
    if not lockfile:
        return []
    pack = lockfile.get("pack") or {}
    target = pack.get("target")
    if target is None:
        return []
    if isinstance(target, list):
        return [str(t).strip() for t in target if str(t).strip()]
    if isinstance(target, str):
        return [t.strip() for t in target.split(",") if t.strip()]
    return []


def _build_info(bundle_dir: Path, *, is_archive: bool, temp_dir: Path | None) -> LocalBundleInfo:
    plugin_json = read_bundle_plugin_json(bundle_dir)
    lockfile = _read_bundle_lockfile(bundle_dir)
    package_id = (plugin_json.get("id") or "").strip() or bundle_dir.name
    return LocalBundleInfo(
        source_dir=bundle_dir,
        plugin_json=plugin_json,
        package_id=package_id,
        lockfile=lockfile,
        pack_targets=_extract_pack_targets(lockfile),
        is_archive=is_archive,
        temp_dir=temp_dir,
    )


def _looks_like_tarball(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".tar.gz") or name.endswith(".tgz")


def _looks_like_legacy_apm_bundle(path: Path) -> bool:
    """Return ``True`` when *path* is a tarball packed with ``--format apm``.

    Legacy bundles contain ``apm.lock.yaml`` at the bundle root but NO
    ``plugin.json``.  This helper extracts the tarball to a temp directory,
    checks for the signal, and cleans up.  Returns ``False`` on any I/O
    error (caller should fall through to the generic error message).
    """
    if not (path.is_file() and _looks_like_tarball(path)):
        return False
    tmp = Path(tempfile.mkdtemp(prefix="apm-legacy-probe-"))
    try:
        _extract_tar_gz_file(path, str(tmp))
        # Locate the inner directory (apm pack uses arcname=<bundle-name>)
        root = tmp
        children = [p for p in tmp.iterdir() if p.is_dir()]
        if len(children) == 1:
            root = children[0]
        return (root / "apm.lock.yaml").is_file() and not (root / "plugin.json").is_file()
    except (ArchiveError, OSError):
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _find_extracted_root(extract_dir: Path) -> Path | None:
    """Find the bundle root inside an extracted tarball.

    Tarballs produced by ``apm pack`` use ``arcname=<bundle-name>``, so
    contents land under ``<extract_dir>/<bundle-name>/``.  Falls back to
    *extract_dir* itself if a top-level ``plugin.json`` is found.
    """
    if (extract_dir / "plugin.json").is_file():
        return extract_dir
    children = [p for p in extract_dir.iterdir() if p.is_dir()]
    if len(children) == 1 and (children[0] / "plugin.json").is_file():
        return children[0]
    for child in children:
        if (child / "plugin.json").is_file():
            return child
    return None


def _extract_zip_bundle(path: Path) -> LocalBundleInfo | None:
    """Extract a ``.zip`` bundle to a temp dir and return :class:`LocalBundleInfo`.

    Applies the same security checks as the tar.gz branch and enforces the ZIP
    size quota while streaming each entry. Returns ``None`` only when the file
    is not a readable ZIP bundle or no ``plugin.json`` root is found; security
    violations raise ``ValueError`` with a targeted reason.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="apm-local-bundle-"))
    try:
        with zipfile.ZipFile(path, "r") as zf:
            safe_extract_zip(
                zf,
                temp_dir,
                max_entries=_MAX_ZIP_ENTRIES,
                max_uncompressed=_MAX_ZIP_UNCOMPRESSED,
                error_type=ValueError,
            )
    except (zipfile.BadZipFile, OSError):
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None
    except ValueError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    bundle_root = _find_extracted_root(temp_dir)
    if bundle_root is None:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None
    return _build_info(bundle_root, is_archive=True, temp_dir=temp_dir)


def detect_local_bundle(path: Path) -> LocalBundleInfo | None:
    """Probe *path*; return :class:`LocalBundleInfo` or ``None``.

    A path qualifies when it is either:

    - A directory containing ``plugin.json`` at its root, OR
    - A ``.zip`` archive whose extracted root contains ``plugin.json``, OR
    - A ``.tar.gz`` / ``.tgz`` archive whose extracted root contains
      ``plugin.json``.

    Archives are extracted to a fresh temporary directory; the caller is
    responsible for cleaning ``info.temp_dir`` after the install completes.
    """
    path = Path(path)
    if not path.exists():
        return None

    if path.is_dir():
        if not (path / "plugin.json").is_file():
            return None
        return _build_info(path, is_archive=False, temp_dir=None)

    if path.is_file() and path.name.lower().endswith(".zip"):
        return _extract_zip_bundle(path)

    if path.is_file() and _looks_like_tarball(path):
        temp_dir = Path(tempfile.mkdtemp(prefix="apm-local-bundle-"))
        try:
            _extract_tar_gz_file(path, str(temp_dir))
        except (ArchiveError, OSError):
            shutil.rmtree(temp_dir, ignore_errors=True)
            return None
        bundle_root = _find_extracted_root(temp_dir)
        if bundle_root is None:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return None
        return _build_info(bundle_root, is_archive=True, temp_dir=temp_dir)

    return None


# ---------------------------------------------------------------------------
# Integrity verification
# ---------------------------------------------------------------------------


def _normalize_hash(value: str) -> str:
    """Strip an optional ``sha256:`` prefix and lowercase the hex digest.

    Raises :class:`ValueError` when the value carries an unsupported
    algorithm prefix (e.g. ``sha512:...``) so callers cannot silently
    accept a hash they will never compute.
    """
    if value.startswith("sha256:"):
        return value[len("sha256:") :].strip().lower()
    if ":" in value:
        raise ValueError(f"Unsupported hash algorithm prefix in: {value!r}")
    return value.strip().lower()


def verify_bundle_integrity(bundle_dir: Path, lockfile: dict[str, Any]) -> list[str]:
    """Walk *bundle_dir* and verify each file against ``pack.bundle_files``.

    Returns a list of human-readable error strings -- empty means the bundle
    is intact.  Symlinks anywhere under *bundle_dir* are always rejected,
    even when not listed in the manifest (a symlink injected after pack
    time is a tampering signal).  Files present in the bundle but absent
    from ``pack.bundle_files`` (other than ``apm.lock.yaml`` and
    ``plugin.json``) are also flagged: the manifest is the source of truth.
    """
    errors: list[str] = []

    # 1) Reject any symlink under the bundle root, regardless of manifest.
    for fp in bundle_dir.rglob("*"):
        if fp.is_symlink():
            rel = fp.relative_to(bundle_dir).as_posix()
            errors.append(f"Symlink rejected in bundle: {rel}")

    # 2) Verify each file listed in pack.bundle_files.
    pack = lockfile.get("pack") or {}
    bundle_files = pack.get("bundle_files") or {}
    if not isinstance(bundle_files, dict):
        errors.append("pack.bundle_files is not a mapping")
        return errors

    listed_rels: set[str] = set()
    for rel, expected in sorted(bundle_files.items()):
        # Reject lockfile-content keys that try to escape the bundle root.
        try:
            validate_path_segments(str(rel), context="bundle_files key")
        except PathTraversalError as exc:
            errors.append(f"Unsafe bundle_files entry {rel!r}: {exc}")
            continue
        target = bundle_dir / rel
        try:
            ensure_path_within(target, bundle_dir)
        except PathTraversalError as exc:
            errors.append(f"Unsafe bundle_files entry {rel!r}: {exc}")
            continue
        listed_rels.add(str(rel))
        if target.is_symlink():
            # Already reported by the symlink sweep above; skip hashing.
            continue
        if not target.is_file():
            errors.append(f"Missing bundle file: {rel}")
            continue
        try:
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError as exc:
            errors.append(f"Cannot read bundle file {rel}: {exc}")
            continue
        try:
            normalized_expected = _normalize_hash(str(expected))
        except ValueError as exc:
            errors.append(f"Invalid hash for {rel}: {exc}")
            continue
        if actual != normalized_expected:
            errors.append(
                f"Hash mismatch for {rel}: expected "
                f"{normalized_expected[:12]}..., got {actual[:12]}..."
            )

    # 3) Detect extra files present in the bundle but not listed in
    # pack.bundle_files.  Anything outside the manifest is a tampering
    # signal -- the only allowed exclusions are the bundle's own
    # apm.lock.yaml and plugin.json.
    _ALLOWED_EXTRAS = {"apm.lock.yaml", "plugin.json"}
    for fp in bundle_dir.rglob("*"):
        if not fp.is_file() or fp.is_symlink():
            continue
        rel = fp.relative_to(bundle_dir).as_posix()
        if rel in _ALLOWED_EXTRAS or rel in listed_rels:
            continue
        errors.append(f"Unlisted bundle file (not in pack.bundle_files): {rel}")

    return errors


# ---------------------------------------------------------------------------
# Target mismatch
# ---------------------------------------------------------------------------


def check_target_mismatch(
    bundle_targets: list[str],
    install_targets: list[str],
) -> str | None:
    """Return a warning string when bundle targets are not covered.

    Returns ``None`` when:

    - ``bundle_targets`` is empty (pre-constraint bundle, no metadata), OR
    - ``bundle_targets`` contains ``"all"`` (target-agnostic bundle), OR
    - ``install_targets`` is a superset of ``bundle_targets``.

    Otherwise returns a human-readable warning naming the missing targets.
    """
    if not bundle_targets:
        return None
    bundle_set = {t.strip() for t in bundle_targets if t and t.strip()}
    # Issue #1207: ``"all"`` (or empty after stripping) means target-agnostic.
    # Such bundles cover any install target, so no mismatch warning is
    # appropriate.
    if "all" in bundle_set:
        return None
    install_set = {t.strip() for t in install_targets if t and t.strip()}
    missing = sorted(bundle_set - install_set)
    if not missing:
        return None
    return (
        "Bundle was packed for targets [{packed}] but install resolved to "
        "[{active}]. The following packed targets will not receive files: "
        "{missing}"
    ).format(
        packed=", ".join(sorted(bundle_set)),
        active=", ".join(sorted(install_set)) or "<none>",
        missing=", ".join(missing),
    )
