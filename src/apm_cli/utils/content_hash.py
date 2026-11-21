"""Deterministic SHA-256 content hashing for package integrity verification."""

import hashlib
import stat
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from apm_cli.install.cache_pin import MARKER_FILENAME as _APM_PIN_MARKER
from apm_cli.utils.atomic_io import normalize_crlf_to_lf
from apm_cli.utils.path_security import PathTraversalError

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


@dataclass(frozen=True)
class PackageDigests:
    """One package tree hashed under both lockfile content-hash streams.

    ``legacy`` is the stream lockfiles at versions 1-3 record; ``framed`` is
    the stream version 4 records. Both come from one read of the same bytes,
    so a lockfile promotion never re-reads a tree it has not verified.
    """

    legacy: str
    framed: str

    def select(self, *, framed: bool) -> str:
        """Return the digest for the stream a lockfile records."""
        return self.framed if framed else self.legacy


def compute_package_digests(
    package_path: Path, *, exclude: Collection[str] = (), legacy: bool = True
) -> PackageDigests:
    """Hash *package_path* once under the framed stream and, unless *legacy* is False, the legacy one.

    The legacy stream feeds each relative path and the file's raw bytes into
    one SHA-256. The framed stream is the encoding the native plugin pin uses
    (:func:`_frame_entry`): NUL-terminated relative path, then the file's own
    SHA-256 digest. Both are platform-invariant only while every file carries
    platform-canonical bytes: git-materialized content already does, and
    every file APM itself writes into the tree MUST be written
    LF-deterministically (``atomic_write_text`` / ``write_text_lf`` /
    ``dump_yaml``). apm#2187/PR #2223 and apm#2619 are the incidents where
    platform-native in-tree writers broke that invariant. Do not normalize
    line endings HERE instead (unlike :func:`compute_file_hash`, apm#1952):
    that would silently change the recorded hash of every package whose
    upstream content legitimately contains CRLF.

    Args:
        package_path: Root directory of the installed package.
        exclude: Package-relative POSIX paths APM itself added after
            materialization (verbatim licence copies, see
            :mod:`apm_cli.deps.carried_license`). Callers pass them from the
            download result or the lockfile record, never from disk, so the
            hash keeps describing the upstream payload.
    """
    legacy_digest, framed = _hash_package_tree(package_path, exclude, legacy=legacy, framed=True)
    return PackageDigests(legacy_digest, framed)


def _hash_package_tree(
    package_path: Path, exclude: Collection[str], *, legacy: bool, framed: bool
) -> tuple[str, str]:
    """Feed only the requested streams; an unrequested stream returns ``""``."""
    if not package_path.is_dir():
        return (_EMPTY_HASH if legacy else ""), (_EMPTY_HASH if framed else "")
    legacy_hasher = hashlib.sha256() if legacy else None
    framed_hasher = hashlib.sha256() if framed else None
    for rel_path in _package_files(package_path):
        if exclude and rel_path.as_posix() in exclude:
            continue
        payload = (package_path / rel_path).read_bytes()
        if legacy_hasher is not None:
            legacy_hasher.update(rel_path.as_posix().encode("utf-8"))
            legacy_hasher.update(payload)
        if framed_hasher is not None:
            framed_hasher.update(_frame_entry(rel_path, payload))
    return (
        f"sha256:{legacy_hasher.hexdigest()}" if legacy_hasher else "",
        f"sha256:{framed_hasher.hexdigest()}" if framed_hasher else "",
    )


def compute_package_hash(
    package_path: Path, *, exclude: Collection[str] = (), framed: bool = True
) -> str:
    """Compute a deterministic SHA-256 hash of a package's file tree.

    The hash covers sorted file paths and their contents, independent of
    filesystem ordering and metadata (timestamps, permissions). *framed*
    selects the stream: lockfile version 4 records the framed stream,
    versions 1-3 the legacy stream (see :func:`compute_package_digests`).
    Callers verifying a lock row pass the row's ``content_hash_framed``.

    Returns:
        Hash string in format ``"sha256:<hex_digest>"``.
    """
    legacy, framed_digest = _hash_package_tree(
        package_path, exclude, legacy=not framed, framed=framed
    )
    return framed_digest if framed else legacy


def _package_files(
    package_path: Path, *, native: bool = False, skip_roots: tuple[str, ...] = ()
) -> list[Path]:
    """Select canonical payload paths without following publisher symlinks."""
    regular_files: list[Path] = []
    excluded = {".git"} if native else _EXCLUDED_DIRS
    for item in package_path.rglob("*"):
        rel = item.relative_to(package_path)
        if any(part in excluded for part in rel.parts) or rel.parts[0] in skip_roots:
            continue
        if item.is_symlink():
            if native:
                raise PathTraversalError(f"Native bundle integrity refuses symlink: {item}")
            continue
        if item.is_file() and not (len(rel.parts) == 1 and rel.name in _EXCLUDED_ROOT_FILES):
            regular_files.append(rel)
    return sorted(regular_files, key=lambda path: path.as_posix())


def compute_native_bundle_hash(package_path: Path) -> str:
    """Fingerprint all native payload bytes and Git executable bits.

    This opt-in contract excludes only Git transport and the root APM pin.
    Existing package/lockfile fingerprints retain their historical semantics.
    """
    if package_path.is_symlink():
        raise PathTraversalError(f"Native bundle integrity refuses symlink: {package_path}")
    return _hash_native_files(package_path, _package_files(package_path, native=True))


# APM's own normalization output in a materialized plugin, never publisher content.
_APM_NORMALIZATION_ROOTS = ("apm.yml", ".apm")


def native_plugin_files(plugin_root: Path) -> list[Path]:
    """Relative paths of the native plugin file set a harness loads.

    The native bundle files minus APM's root ``apm.yml`` and root ``.apm/``,
    which legacy-plugin normalization synthesizes under ``apm_modules``.
    """
    return _package_files(plugin_root, native=True, skip_roots=_APM_NORMALIZATION_ROOTS)


def compute_native_plugin_hash(plugin_root: Path) -> str:
    """Per-machine integrity hash of the native plugin file set: path, exec bit and bytes.

    Compares a deployed copy with its source on the same machine. Approval
    pins use :func:`compute_native_plugin_pin`, which leaves the exec bit out.
    """
    if plugin_root.is_symlink():
        raise PathTraversalError(f"Native plugin integrity refuses symlink: {plugin_root}")
    return _hash_native_files(plugin_root, native_plugin_files(plugin_root))


def compute_native_plugin_pin(plugin_root: Path) -> str:
    """Portable approval pin of the native plugin file set: path and bytes.

    The exec bit is left out: Windows derives it from the file extension
    (``.cmd``, ``.bat``, ``.exe``) rather than the Git mode, so a pin that
    included it would differ across operating systems and an approval
    committed on one would park on the other.
    """
    if plugin_root.is_symlink():
        raise PathTraversalError(f"Native plugin integrity refuses symlink: {plugin_root}")
    return _hash_native_files(plugin_root, native_plugin_files(plugin_root), include_mode=False)


def unpinned_apm_copy(plugin_root: Path) -> str | None:
    """Name a root ``.apm/`` file that is not a byte copy of a native plugin file.

    APM's normalization only copies plugin files into ``.apm/``, so every
    file there must equal some file :func:`native_plugin_files` covers;
    anything else is content the plugin pin would not bind. The one exception
    is the skill-source receipt legacy-plugin normalization writes beside the
    copies, and only when it is exactly the canonical receipt for this root.
    An Agent Plugin is loaded live and never normalized, so it gets none.
    """
    from apm_cli.deps.plugin_parser import canonical_normalization_receipt

    apm_dir = plugin_root / ".apm"
    if not apm_dir.exists():
        return None
    receipt = canonical_normalization_receipt(plugin_root)
    try:
        pinned = {
            hashlib.sha256((plugin_root / relative).read_bytes()).digest()
            for relative in native_plugin_files(plugin_root)
        }
        for path in sorted(apm_dir.rglob("*")):
            label = path.relative_to(plugin_root).as_posix()
            if path.is_symlink():
                return f"'{label}' is a symlink"
            if receipt is not None and path == receipt[0] and path.read_bytes() == receipt[1]:
                continue
            if path.is_file() and hashlib.sha256(path.read_bytes()).digest() not in pinned:
                return f"'{label}' is not a copy of a pinned plugin file"
    except (OSError, ValueError):
        return "'.apm/' cannot be read"
    return None


def _frame_entry(relative: Path, payload: bytes, mode: bytes = b"") -> bytes:
    """Frame one file: NUL-terminated relative path, optional mode, own digest.

    The single framing encoding behind every APM tree fingerprint. The NUL
    terminator and the fixed-length per-file digest keep entry boundaries
    unambiguous, so distinct file layouts cannot share one byte stream.
    """
    return relative.as_posix().encode("utf-8") + b"\0" + mode + hashlib.sha256(payload).digest()


def _hash_native_files(root: Path, files: list[Path], *, include_mode: bool = True) -> str:
    hasher = hashlib.sha256()
    for relative in files:
        path = root / relative
        mode = b"x" if path.stat().st_mode & stat.S_IXUSR else b"-"
        hasher.update(_frame_entry(relative, path.read_bytes(), mode if include_mode else b""))
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


def content_hash_of_bytes(raw: bytes) -> str:
    """Hash *raw* as :func:`compute_file_hash` hashes a file holding it."""
    return f"sha256:{hashlib.sha256(_canonical_hash_bytes(raw)).hexdigest()}"


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


def verify_package_hash(
    package_path: Path,
    expected_hash: str,
    *,
    exclude: Collection[str] = (),
    framed: bool,
) -> bool:
    """Verify a package's content matches the expected hash.

    Args:
        package_path: Root directory of the installed package.
        expected_hash: Expected hash string (e.g., ``"sha256:abc123..."``).
        exclude: Carried licence paths, as for :func:`compute_package_hash`.
        framed: The stream *expected_hash* was recorded in; a lock row's
            ``content_hash_framed``.

    Returns:
        True if hash matches, False if mismatch.
    """
    return same_package_digest(
        compute_package_hash(package_path, exclude=exclude, framed=framed), expected_hash
    )


def same_package_digest(left: str, right: str) -> bool:
    """Compare two SHA-256 digests, accepting bare hex and the ``sha256:`` envelope alike."""

    def _hex(value: str) -> str:
        return value.strip().lower().removeprefix("sha256:")

    return bool(left) and bool(right) and _hex(left) == _hex(right)
