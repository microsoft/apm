"""Lockfile assembly: build a ``LockFile`` from install artefacts.

This module hosts the ``LockfileBuilder`` that assembles a
:class:`~apm_cli.deps.lockfile.LockFile` from the artefacts produced by
earlier install phases (deployed files, types, hashes, marketplace
provenance, dependency graph).

Currently exposes only ``compute_deployed_hashes()`` — the per-file
content-hash helper relocated from ``commands/install.py``
(:pypi:`#762`).  P2.S6 will fold the inline lockfile assembly logic
that lives inside ``_install_apm_dependencies`` into
:class:`LockfileBuilder`.
"""

from pathlib import Path

from apm_cli.utils.content_hash import compute_file_hash


def compute_deployed_hashes(rel_paths, project_root: Path) -> dict:
    """Hash currently-on-disk deployed files for provenance.

    Module-level so both the local-package persist site (in
    ``_integrate_local_content``) and the remote-package lockfile-build
    site (in ``_install_apm_dependencies``) share one implementation.
    Returns ``{rel_path: "sha256:<hex>"}`` for files that exist as regular
    files; symlinks and unreadable paths are silently omitted (they cannot
    contribute meaningful provenance).
    """
    out: dict = {}
    for _rel in rel_paths or ():
        _full = project_root / _rel
        if _full.is_file() and not _full.is_symlink():
            try:
                out[_rel] = compute_file_hash(_full)
            except Exception:
                pass
    return out


class LockfileBuilder:
    """Incrementally assembles a ``LockFile`` from install artefacts.

    Currently a thin skeleton that delegates to
    :func:`compute_deployed_hashes`.  The following builder methods will
    be added in **P2.S6** when the inline lockfile assembly logic inside
    ``_install_apm_dependencies`` is folded in:

    - ``with_installed(dep_key, locked_dep)``
    - ``with_deployed_files(dep_key, files)``
    - ``with_types(dep_key, package_type)``
    - ``with_provenance(dep_key, marketplace_info)``
    - ``build() -> LockFile``
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def compute_deployed_hashes(self, rel_paths) -> dict[str, str]:
        """Delegate to the module-level canonical implementation."""
        return compute_deployed_hashes(rel_paths, self.project_root)
