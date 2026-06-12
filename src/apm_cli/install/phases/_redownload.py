"""Shared guard for the content-hash skip-redownload fallback (#763, #768).

When a package's ``.git`` directory has been removed, the install pipeline
cannot compare local HEAD against ``locked_dep.resolved_commit``.  In that
case it falls back to a content-hash check: if the lockfile recorded a
``content_hash`` for the package AND the install path is still a directory,
re-hashing the on-disk content and comparing against the lockfile value is
enough to confirm the package is intact and skip re-downloading.

This module exposes :func:`_should_skip_redownload` so the three call sites
(parallel pre-download and two branches of the sequential integrate loop)
share one auditable definition.  Tests call it directly -- guard mutations
in this function MUST surface as test failures (mutation-break safety).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _should_skip_redownload(locked_dep: Any, install_path: Path) -> bool:
    """Return True when ``install_path`` content matches ``locked_dep.content_hash``.

    Returns False when ``locked_dep`` is missing/has no ``content_hash``, when
    ``install_path`` is not an existing directory, or when the on-disk content
    does not hash to the recorded value.  Callers use the True return to skip
    a redundant re-download after the git-based check fails (#763).
    """
    if locked_dep is None or not locked_dep.content_hash:
        return False
    if not install_path.is_dir():
        return False

    from apm_cli.utils.content_hash import verify_package_hash

    return verify_package_hash(install_path, locked_dep.content_hash)
