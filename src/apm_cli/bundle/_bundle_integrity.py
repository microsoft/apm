"""Bundle integrity verification helpers.

Extracted from local_bundle to keep that module under 400 lines.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..utils.path_security import PathTraversalError, ensure_path_within, validate_path_segments


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


def _verify_listed_files(bundle_dir: Path, bundle_files: dict) -> tuple[list[str], set[str]]:
    """Verify each listed file in *bundle_files* against *bundle_dir*.

    Returns ``(errors, listed_rels)`` where *errors* is a list of human-readable
    error strings and *listed_rels* is the set of relative paths that were
    checked.
    """
    errors: list[str] = []
    listed_rels: set[str] = set()

    for rel, expected in sorted(bundle_files.items()):
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

    return errors, listed_rels


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

    file_errors, listed_rels = _verify_listed_files(bundle_dir, bundle_files)
    errors.extend(file_errors)

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
