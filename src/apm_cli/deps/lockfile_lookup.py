"""Lockfile row lookup by dependency reference (#4037).

Lives beside ``lockfile.py`` (which is at its file-length cap) and reads
rows only through ``LockFile.get_dependency`` and the key that
``LockedDependency.from_dependency_ref`` writes, so the lockfile stays the
single owner of row keying.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models.dependency.reference import DependencyReference
    from .lockfile import LockedDependency, LockFile


def locked_row_for(lock: LockFile, dep_ref: DependencyReference) -> LockedDependency | None:
    """Return the row :meth:`LockedDependency.from_dependency_ref` wrote for *dep_ref*.

    An explicit proxy URL (``host/artifactory/...``) written without a
    registry-proxy config keeps its proxy host and no ``registry_prefix``,
    so its row key is host-qualified while the reference key stays bare.
    A bare row without ``registry_prefix`` is another package.
    """
    from .lockfile import LockedDependency

    row = lock.get_dependency(dep_ref.get_unique_key())
    owner = getattr(dep_ref, "ownership_reference", dep_ref)
    if not getattr(owner, "artifactory_prefix", None) or (row and row.registry_prefix):
        return row
    written = LockedDependency.from_dependency_ref(owner, None, 1, None)
    return lock.get_dependency(written.get_unique_key())
