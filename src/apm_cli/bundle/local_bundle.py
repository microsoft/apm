"""Local-bundle detection, integrity verification, and target-mismatch checks.

This module powers ``apm install <local-bundle-path>`` (issue #1098).  A local
bundle is a directory or ``.tar.gz`` produced by ``apm pack`` -- it contains a
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

import json
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

from ..utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)


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
        is_archive: ``True`` when the source path was a ``.tar.gz``.
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


def _looks_like_archive(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".tar.gz") or name.endswith(".tgz")


def _looks_like_legacy_apm_bundle(path: Path) -> bool:
    """Return ``True`` when *path* is a tarball packed with ``--format apm``.

    Legacy bundles contain ``apm.lock.yaml`` at the bundle root but NO
    ``plugin.json``.  This helper extracts the tarball to a temp directory,
    checks for the signal, and cleans up.  Returns ``False`` on any I/O
    error (caller should fall through to the generic error message).
    """
    if not (path.is_file() and _looks_like_archive(path)):
        return False
    tmp = Path(tempfile.mkdtemp(prefix="apm-legacy-probe-"))
    try:
        with tarfile.open(path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.issym() or member.islnk():
                    return False
                name = member.name
                if (
                    name.startswith("/")
                    or PureWindowsPath(name).drive
                    or PureWindowsPath(name).is_absolute()
                ):
                    return False
                try:
                    validate_path_segments(name, context="tar member")
                except PathTraversalError:
                    return False
            if sys.version_info >= (3, 12):
                tar.extractall(tmp, filter="data")
            else:
                tar.extractall(tmp)  # noqa: S202 -- validated above
        # Locate the inner directory (apm pack uses arcname=<bundle-name>)
        root = tmp
        children = [p for p in tmp.iterdir() if p.is_dir()]
        if len(children) == 1:
            root = children[0]
        return (root / "apm.lock.yaml").is_file() and not (root / "plugin.json").is_file()
    except (tarfile.TarError, OSError):
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


def _validate_and_extract_tarball(path: Path, temp_dir: Path) -> bool:
    """Validate and extract *path* (a .tar.gz archive) into *temp_dir*.

    Returns ``True`` on success, ``False`` on any security or IO error.
    Cleans up *temp_dir* on failure.
    """
    try:
        with tarfile.open(path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.issym() or member.islnk():
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    return False
                name = member.name
                if (
                    name.startswith("/")
                    or PureWindowsPath(name).drive
                    or PureWindowsPath(name).is_absolute()
                ):
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    return False
                try:
                    validate_path_segments(name, context="tar member")
                except PathTraversalError:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    return False
            if sys.version_info >= (3, 12):
                tar.extractall(temp_dir, filter="data")
            else:
                tar.extractall(temp_dir)  # noqa: S202 -- validated above
    except (tarfile.TarError, OSError):
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False
    return True


from ._bundle_integrity import (
    _normalize_hash,
    _verify_listed_files,
)
from ._bundle_integrity import (
    verify_bundle_integrity as verify_bundle_integrity,
)


def detect_local_bundle(path: Path) -> LocalBundleInfo | None:
    """Probe *path*; return :class:`LocalBundleInfo` or ``None``.

    A path qualifies when it is either:

    - A directory containing ``plugin.json`` at its root, OR
    - A ``.tar.gz`` / ``.tgz`` archive whose extracted root contains
      ``plugin.json``.

    Tarballs are extracted to a fresh temporary directory; the caller is
    responsible for cleaning ``info.temp_dir`` after the install completes.
    """
    path = Path(path)
    if not path.exists():
        return None

    if path.is_dir():
        if not (path / "plugin.json").is_file():
            return None
        return _build_info(path, is_archive=False, temp_dir=None)

    if path.is_file() and _looks_like_archive(path):
        temp_dir = Path(tempfile.mkdtemp(prefix="apm-local-bundle-"))
        if not _validate_and_extract_tarball(path, temp_dir):
            return None
        bundle_root = _find_extracted_root(temp_dir)
        if bundle_root is None:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return None
        return _build_info(bundle_root, is_archive=True, temp_dir=temp_dir)

    return None


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
