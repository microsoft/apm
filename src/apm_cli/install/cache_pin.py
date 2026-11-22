"""Cache-pin marker for drift-replay correctness.

When ``apm install`` populates ``apm_modules/<owner>/<repo>/`` from a
specific lockfile pin, it drops a small JSON marker (``.apm-pin``) at
the package root recording the ``resolved_commit`` that produced the
cache contents.

``apm audit`` drift-replay then verifies the marker matches the
lockfile's ``resolved_commit`` BEFORE diffing. This catches a
critical correctness hazard:

* Shared CI runners that retain ``apm_modules/`` across builds.
* A teammate updating ``apm.lock.yaml`` (e.g. bumping a pin from X to
  Y) without re-running ``apm install``.

Without the marker, drift-replay would happily diff the new lockfile
against stale cache content and report meaningless or misleading
findings.

Threat model -- this is "stale-cache detection", NOT cryptographic
integrity. An adversary with write access to ``apm_modules/`` can
always tamper with both the cache content AND the marker. Defending
against active cache tampering requires content-addressed hashes /
signatures, which is deferred. The marker closes the
honest-mistake gap that motivated this work.

Schema (v1)::

    {"schema_version": 1, "resolved_commit": "<git-sha-or-similar>"}

Future schema bumps must keep ``schema_version`` parseable so older
clients can fail closed with a clear message.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency, LockFile


MARKER_FILENAME = ".apm-pin"
SCHEMA_VERSION = 1


class CachePinError(RuntimeError):
    """Raised when the cache pin is missing, malformed, or stale.

    The orchestrator (drift replay) catches this and translates it to
    a ``CacheMissError`` with the same message, so the user-facing
    advice ("run apm install") is consistent across every cache
    inconsistency reason.
    """


def write_marker(install_path: Path, resolved_commit: str) -> None:
    """Write the cache-pin marker file to ``install_path``.

    Idempotent: callers may invoke this on every install, regardless of
    whether the lockfile YAML changed, to self-heal caches that pre-date
    the marker contract. An identical regular-file marker is left untouched,
    so a converged install writes nothing (#4790). Otherwise the marker is
    written atomically as a regular file (temp file in the same directory,
    then ``os.replace``), replacing whatever entry was there without ever
    following a link.

    Failures are silent at the filesystem layer because they are
    non-fatal for ``apm install`` itself -- a missing or unwritable
    marker is detected and surfaced by the drift-replay verify step.
    The caller (``LockfileBuilder``) cannot stop a successful install
    just because we could not annotate its cache.
    """
    if not install_path.exists() or not install_path.is_dir():
        return
    payload = {
        "schema_version": SCHEMA_VERSION,
        "resolved_commit": resolved_commit,
    }
    marker = install_path / MARKER_FILENAME
    data = json.dumps(payload).encode("utf-8")
    try:
        current = _regular_marker_bytes(marker)
    except OSError:
        current = None  # unreadable: treat as a miss and still repair it
    if current == data:
        return
    try:
        _replace_marker(install_path, marker, data)
    except OSError:
        # Read-only mount, permission denied, a directory in the way, etc.
        # Drift will surface this on the next audit; install must not fail.
        return


def _regular_marker_bytes(marker: Path) -> bytes | None:
    """The marker's bytes when it is a regular file, read without following links."""
    try:
        info = os.lstat(marker)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    fd = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _replace_marker(install_path: Path, marker: Path, data: bytes) -> None:
    """Write *data* to a temp file beside *marker*, then rename it over the marker."""
    fd, tmp = tempfile.mkstemp(dir=install_path, prefix=f"{MARKER_FILENAME}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, marker)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def verify_marker(install_path: Path, expected_commit: str) -> None:
    """Verify the marker at ``install_path`` matches ``expected_commit``.

    Raises
    ------
    CachePinError
        On any of: marker file absent, unreadable, malformed JSON,
        unsupported ``schema_version``, missing ``resolved_commit``
        field, or commit mismatch. The exception message is
        user-facing -- it is rendered verbatim by ``apm audit``.
    """
    marker = install_path / MARKER_FILENAME
    if not marker.exists():
        raise CachePinError(
            f"cache pin marker missing at {marker} -- cache pre-dates supply-chain hardening"
        )
    try:
        raw = marker.read_text(encoding="utf-8")
    except OSError as exc:
        raise CachePinError(f"cache pin marker unreadable at {marker}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CachePinError(f"cache pin marker at {marker} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CachePinError(
            f"cache pin marker at {marker} must be a JSON object, got {type(payload).__name__}"
        )
    schema = payload.get("schema_version")
    if schema != SCHEMA_VERSION:
        raise CachePinError(
            f"cache pin marker at {marker} has unsupported schema_version "
            f"{schema!r}; expected {SCHEMA_VERSION}"
        )
    actual = payload.get("resolved_commit")
    if not isinstance(actual, str) or not actual:
        raise CachePinError(f"cache pin marker at {marker} is missing resolved_commit")
    if actual != expected_commit:
        raise CachePinError(
            f"cache pin mismatch at {marker}: "
            f"marker says {actual!r}, lockfile expects {expected_commit!r}"
        )


def find_unpinned_remote_deps(lockfile: LockFile) -> list[str]:
    """Return repo_url for every remote dep that lacks a ``resolved_commit``.

    A remote dep without a pinned commit is a supply-chain weakness:

    * The cache could come from any commit on the referenced branch/tag.
    * Drift replay cannot verify the cache matches the lockfile.
    * No marker can be written, so future drift runs cannot fail-closed.

    Callers (``sync_markers_for_lockfile`` at install, ``_materialize_install_path``
    at audit) should not silently no-op these entries -- they should
    warn on install and fail-closed on audit.
    """
    unpinned: list[str] = []
    for _key, dep in lockfile.dependencies.items():
        if getattr(dep, "source", None) in {"local", "registry"}:
            continue
        commit = getattr(dep, "resolved_commit", None)
        if not isinstance(commit, str) or not commit:
            unpinned.append(getattr(dep, "repo_url", "") or _key)
    return unpinned


def sync_markers_for_lockfile(
    lockfile: LockFile,
    project_root: Path,
    apm_modules_dir: Path,
    *,
    warn_unpinned: bool = True,
) -> int:
    """Write a marker for every remote dep in ``lockfile`` that has a cached install.

    A dep that is no longer markable (registry, local, unpinned) loses any
    marker its copy kept from a previous source (#4572), so a git -> registry
    switch converges on the first install.

    Self-healing: this is called unconditionally by ``LockfileBuilder``
    after ``_write_if_changed`` so that caches which pre-date the
    marker contract get marked on the next ``apm install`` even when
    the lockfile YAML itself does not need to be rewritten.

    Side-effect: any remote dep that lacks a ``resolved_commit`` produces
    a single stderr warning (one per dep). These deps cannot participate
    in stale-cache verification, which is a supply-chain weakness worth
    surfacing -- silent no-op would let unpinned refs sneak past audit.
    Pass ``warn_unpinned=False`` to suppress these warnings when this is a
    secondary self-heal pass over the pre-existing lockfile (the primary
    pass over the freshly-built lockfile already surfaces them, so warning
    twice would be noise).

    Returns the count of markers written (useful for verbose logging
    and for tests).
    """
    if warn_unpinned:
        unpinned = find_unpinned_remote_deps(lockfile)
        if unpinned:
            from apm_cli.core.remediation import REMOTE_DEPENDENCY_UNPINNED
            from apm_cli.utils.console import _rich_warning

            for repo in unpinned:
                _rich_warning(
                    f"cache-pin: remote dep {repo!r} has no resolved_commit; "
                    "drift cannot verify its cache freshness. "
                    f"{REMOTE_DEPENDENCY_UNPINNED.sentence()}"
                )

    written = 0
    marked: set[Path] = set()
    unmarkable: list[Path] = []
    for dep_key, dep in lockfile.dependencies.items():  # noqa: B007
        try:
            install_path = _resolve_install_path(dep, project_root, apm_modules_dir)
        except Exception:  # noqa: S112 -- best-effort marker sync, see module docstring
            continue
        if install_path is None or not install_path.exists():
            continue
        if not _is_markable(dep):
            unmarkable.append(install_path)
            continue
        marked.add(install_path)
        if not _copy_holds_locked_commit(dep, install_path):
            continue
        # dep.resolved_commit is non-None per _is_markable.
        write_marker(install_path, dep.resolved_commit)  # type: ignore[arg-type]
        written += 1
    for install_path in unmarkable:
        if install_path not in marked:
            _remove_marker(install_path, apm_modules_dir)
    return written


def _remove_marker(install_path: Path, apm_modules_dir: Path) -> None:
    """Drop a marker left by a previous source of an unmarkable dep (#4572).

    A registry or local dep records no resolved commit, so a marker in its
    copy names a commit the lockfile no longer vouches for. Only copies under
    ``apm_modules`` are touched. Best-effort, like ``write_marker``: a removal
    failure never blocks install.
    """
    try:
        if not install_path.resolve().is_relative_to(apm_modules_dir.resolve()):
            return
        (install_path / MARKER_FILENAME).unlink(missing_ok=True)
    except OSError:
        return


def _copy_holds_locked_commit(dep: LockedDependency, install_path: Path) -> bool:
    """Whether the copy may carry a marker naming *dep*'s locked commit (#3884).

    The resolver trusts the marker, so the copy-validity owner decides: a
    proven copy, or a legacy copy with no evidence against it. A copy left by
    an older lock, or a checkout at another commit, stays unmarked.
    """
    from apm_cli.drift import locked_copy_is_stale

    return not locked_copy_is_stale(dep, install_path)


def _is_markable(dep: LockedDependency) -> bool:
    """A dep is markable when it has a deterministic remote pin we can verify."""
    if getattr(dep, "source", None) in {"local", "registry"}:
        return False
    commit = getattr(dep, "resolved_commit", None)
    return isinstance(commit, str) and bool(commit)


def _resolve_install_path(
    dep: LockedDependency,
    project_root: Path,
    apm_modules_dir: Path,
) -> Path | None:
    """Resolve the on-disk install path for a remote dep without raising."""
    try:
        dep_ref = dep.to_dependency_ref()
    except Exception:
        return None
    try:
        return dep_ref.get_install_path(apm_modules_dir)
    except Exception:
        return None


__all__ = [
    "MARKER_FILENAME",
    "SCHEMA_VERSION",
    "CachePinError",
    "find_unpinned_remote_deps",
    "sync_markers_for_lockfile",
    "verify_marker",
    "write_marker",
]
