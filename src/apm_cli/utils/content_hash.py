"""Deterministic SHA-256 content hashing for package integrity verification."""

import hashlib
from pathlib import Path

from apm_cli.install.cache_pin import MARKER_FILENAME as _APM_PIN_MARKER
from apm_cli.utils.atomic_io import normalize_crlf_to_lf

# Directories excluded from hashing (not relevant to package content)
_EXCLUDED_DIRS = {".git", "__pycache__"}

# Files at the package root excluded from hashing. ``.apm-pin`` is the
# cache-pin marker (see :mod:`apm_cli.install.cache_pin`) written AFTER
# hash recording during install; including it would make the on-disk
# hash diverge from the lockfile-recorded hash on every subsequent
# install, falsely tripping the supply-chain content-hash mismatch
# check. Scoped to root paths only so a package cannot slip a
# ``subdir/.apm-pin`` past the integrity hash.
_EXCLUDED_ROOT_FILES = {_APM_PIN_MARKER}

# Well-known hash for empty/missing packages
_EMPTY_HASH = "sha256:" + hashlib.sha256(b"").hexdigest()


def compute_package_hash(package_path: Path) -> str:
    """Compute a deterministic SHA-256 hash of a package's file tree.

    The hash is computed over sorted file paths and their contents,
    making it independent of filesystem ordering and metadata (timestamps,
    permissions).

    Note: this whole-tree hash intentionally hashes raw file bytes, unlike
    the per-file :func:`compute_file_hash` which normalizes CRLF->LF for
    text (apm#1952). The package tree is hashed at the git-checkout
    boundary where content is already platform-canonical, and the path is
    bound into the digest, so cross-platform line-ending identity is
    unnecessary here. Do not unify the two without re-checking that
    invariant.

    Args:
        package_path: Root directory of the installed package.

    Returns:
        Hash string in format ``"sha256:<hex_digest>"``.
    """
    if not package_path.is_dir():
        return _EMPTY_HASH

    hasher = hashlib.sha256()
    file_count = 0

    # Collect all regular files, skipping excluded dirs and symlinks
    regular_files: list[Path] = []
    for item in package_path.rglob("*"):
        # Skip symlinks
        if item.is_symlink():
            continue
        # Skip excluded directories and their contents
        rel = item.relative_to(package_path)
        if any(part in _EXCLUDED_DIRS for part in rel.parts):
            continue
        if item.is_file():
            if len(rel.parts) == 1 and rel.name in _EXCLUDED_ROOT_FILES:
                continue
            regular_files.append(rel)

    # Sort lexicographically by POSIX path for determinism
    regular_files.sort(key=lambda p: p.as_posix())

    for rel_path in regular_files:
        # Hash the relative path then the file contents
        hasher.update(rel_path.as_posix().encode("utf-8"))
        hasher.update((package_path / rel_path).read_bytes())
        file_count += 1

    if file_count == 0:
        return _EMPTY_HASH

    return f"sha256:{hasher.hexdigest()}"


def _canonical_hash_bytes(raw: bytes) -> bytes:
    """Return the canonical byte image used for per-file content hashing.

    Text content (UTF-8-decodable and free of NUL bytes) is line-ending
    normalized CRLF -> LF, with bare CR preserved, so the deployed-file
    hash is platform-invariant (apm#1952): a file that git materializes
    as ``\\r\\n`` on Windows and ``\\n`` on POSIX hashes identically.
    Detection is content-based (not suffix-based) so integrator renames
    such as ``.md`` -> ``.mdc`` are still normalized.

    Binary content -- anything that is not valid UTF-8, or that contains
    a NUL byte (e.g. UTF-16, images) -- is hashed raw; normalizing it
    would be meaningless and could corrupt the integrity witness.

    Preserving bare CR (only ``\\r\\n`` collapses to ``\\n``) keeps the
    carriage-return smuggling vector hash-visible: solely the benign
    line-terminator difference is made invisible, never a lone ``\\r``
    that a terminal or parser could interpret as an overwrite. On line
    endings this aligns with the drift-replay normalizer
    (:func:`apm_cli.utils.normalization._normalize_line_endings`), so the
    ``content-integrity`` audit and the drift-replay audit no longer
    disagree about whether a CRLF/LF difference is a content change.
    (Drift-replay additionally strips BOM and build-id markers; this
    per-file integrity hash deliberately does not -- those remain
    hash-visible here.)
    """
    if b"\x00" in raw:
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    return normalize_crlf_to_lf(text).encode("utf-8")


def compute_file_hash(file_path: Path) -> str:
    """Compute SHA-256 of a single file's content (line-ending-normalized).

    Used for per-deployed-file provenance checks before APM deletes a
    file recorded in ``deployed_files``. The path itself is not mixed
    in (unlike :func:`compute_package_hash`) because deployed files may
    be renamed by integrators (e.g. ``.md`` -> ``.mdc`` for Cursor).

    UTF-8 text content is hashed over its CRLF -> LF normalized form
    (bare CR preserved) so the hash is identical on every platform,
    regardless of whether git ``core.autocrlf`` materialized the file
    with ``\\n`` or ``\\r\\n`` (apm#1952). This makes the record side
    (``apm install``) and the verify side (``apm audit``) symmetric by
    construction across operating systems. Binary content (non-UTF-8 or
    containing a NUL byte) is hashed raw. See :func:`_canonical_hash_bytes`
    for the security rationale.

    Args:
        file_path: File to hash.

    Returns:
        Hash string in format ``"sha256:<hex_digest>"``. Returns the
        empty-content hash when the path does not exist or is not a
        regular file.
    """
    if not file_path.is_file() or file_path.is_symlink():
        return _EMPTY_HASH
    hasher = hashlib.sha256()
    hasher.update(_canonical_hash_bytes(file_path.read_bytes()))
    return f"sha256:{hasher.hexdigest()}"


def verify_package_hash(package_path: Path, expected_hash: str) -> bool:
    """Verify a package's content matches the expected hash.

    Args:
        package_path: Root directory of the installed package.
        expected_hash: Expected hash string (e.g., ``"sha256:abc123..."``).

    Returns:
        True if hash matches, False if mismatch.
    """
    actual = compute_package_hash(package_path)
    return actual == expected_hash
