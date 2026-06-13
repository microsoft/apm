"""Canonical resolution of a locked LOCAL dependency's on-disk source directory.

Both the install resolver (:mod:`apm_cli.deps.apm_resolver`) and the
drift-replay engine (:mod:`apm_cli.install.drift`) must turn a local
dependency's ``local_path`` into the real directory on disk. They MUST
agree byte-for-byte: if they diverge, ``apm audit`` reports drift that
``apm install`` never produced -- or, worse, silently skips drift the
install actually deployed (the failure mode this module was written to
kill).

Anchoring rules (mirroring ``install/phases/local_content.py`` and
``apm_resolver._compute_dep_source_path``, issue #857):

* a direct (root-declared) local dep stores a project-root-relative
  ``local_path`` (``./packages/foo``) and ``resolved_by = None`` -- it
  anchors on ``project_root``;
* a transitive local dep stores a path relative to the package that
  DECLARED it (``../sibling``) plus ``resolved_by`` = that parent's
  ``repo_url`` -- it anchors on the parent's resolved directory,
  recursively up to a root dep;
* an absolute ``local_path`` bypasses anchoring entirely.

No project-root containment clamp is applied here: install deliberately
permits out-of-tree local sources (``apm install ../pkg-a`` from a
monorepo workspace; see ``local_content._copy_local_package``), and the
untrusted-source boundary is the resolver-level expansion/rejection of
``local_path`` deps declared by REMOTE parents (issues #940 / #1571),
enforced upstream before a dep ever reaches the lockfile. Drift simply
mirrors install so audit and install never disagree.

A genuinely corrupt lockfile -- a ``resolved_by`` parent that is missing,
ambiguous, non-local, or part of a cycle -- raises
:class:`LocalResolutionError`. Callers MUST surface that as a hard
failure and never fold it into a benign "cache not populated" skip:
swallowing it is exactly how a resolution bug silently disables drift
detection repo-wide.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency, LockFile


class LocalResolutionError(Exception):
    """A locked local dependency cannot be anchored from the lockfile graph.

    Signals an internally inconsistent lockfile (missing / ambiguous /
    non-local / cyclic ``resolved_by`` parent), NOT a cold cache. It is
    deliberately NOT a subclass of
    :class:`apm_cli.install.drift.CacheMissError` so the drift gate's
    ``except CacheMissError`` soft-skip cannot swallow it.
    """


def resolve_local_dep_dir(
    lock_dep: LockedDependency,
    lockfile: LockFile | None,
    project_root: Path,
) -> Path:
    """Resolve the on-disk source directory for a locked LOCAL dependency.

    Walks the ``resolved_by`` chain to anchor a transitive ``../sibling``
    path on the directory of the package that declared it, recursively up
    to a root-declared dep which anchors on ``project_root``. Absolute
    ``local_path`` values bypass anchoring.

    The returned path is NOT checked for existence -- that is the caller's
    concern (a missing-but-correctly-resolved source is a cold-cache-like
    condition, distinct from the corrupt-lockfile conditions raised here).

    Raises
    ------
    LocalResolutionError
        If *lock_dep* is not a local dep, has no ``local_path``, or its
        ``resolved_by`` chain references a parent that is missing,
        ambiguous, non-local, or cyclic.
    """
    if lock_dep.source != "local" or not lock_dep.local_path:
        raise LocalResolutionError(
            f"not a resolvable local dependency: repo_url={lock_dep.repo_url!r} "
            f"source={lock_dep.source!r} local_path={lock_dep.local_path!r}"
        )
    anchor = _anchor_dir(lock_dep, lockfile, project_root.resolve(), seen=set())
    return _join(anchor, lock_dep.local_path)


def _join(anchor: Path, local_path: str) -> Path:
    """Resolve *local_path* against *anchor*, leaving absolute paths alone."""
    raw = Path(local_path).expanduser()
    return raw.resolve() if raw.is_absolute() else (anchor / raw).resolve()


def _anchor_dir(
    dep: LockedDependency,
    lockfile: LockFile | None,
    project_root: Path,
    seen: set[str],
) -> Path:
    """Return the directory on which *dep*'s ``local_path`` is anchored."""
    if not dep.resolved_by:
        return project_root

    key = dep.get_unique_key()
    if key in seen:
        raise LocalResolutionError(
            f"cycle in resolved_by chain at {dep.repo_url!r} "
            f"(local_path={dep.local_path!r}, resolved_by={dep.resolved_by!r})"
        )
    seen.add(key)

    parent = _find_parent(lockfile, dep)
    parent_anchor = _anchor_dir(parent, lockfile, project_root, seen)
    return _join(parent_anchor, parent.local_path)


def _find_parent(lockfile: LockFile | None, dep: LockedDependency) -> LockedDependency:
    """Find the unique local parent of *dep* by ``repo_url == resolved_by``.

    ``resolved_by`` carries the parent's ``repo_url`` (e.g. ``_local/foo``),
    NOT its ``local_path``; and the lockfile dict is keyed by
    ``get_unique_key()`` (``local_path`` for locals), so the parent cannot
    be looked up by key -- we scan by ``repo_url``. Because ``_local/<name>``
    is basename-derived it is NOT guaranteed unique, so a duplicate match is
    a hard error rather than a silent first-wins pick.
    """
    if lockfile is None:
        raise LocalResolutionError(
            f"{dep.repo_url!r} declares resolved_by={dep.resolved_by!r} but no "
            "lockfile was supplied to resolve the parent"
        )
    matches = [
        other
        for other in lockfile.dependencies.values()
        if other.repo_url == dep.resolved_by and other.source == "local" and other.local_path
    ]
    if not matches:
        raise LocalResolutionError(
            f"resolved_by parent {dep.resolved_by!r} of {dep.repo_url!r} "
            f"(local_path={dep.local_path!r}) is not a local dependency in the lockfile"
        )
    if len(matches) > 1:
        raise LocalResolutionError(
            f"ambiguous resolved_by parent {dep.resolved_by!r} of {dep.repo_url!r}: "
            f"{len(matches)} local dependencies share that repo_url "
            f"({sorted(m.local_path for m in matches if m.local_path)})"
        )
    return matches[0]
