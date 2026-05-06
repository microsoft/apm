"""Lockfile assembly: build a ``LockFile`` from install artefacts.

This module hosts the ``LockfileBuilder`` that assembles a
:class:`~apm_cli.deps.lockfile.LockFile` from the artefacts produced by
earlier install phases (deployed files, types, hashes, marketplace
provenance, dependency graph).

Exposes:

- ``compute_deployed_hashes()`` -- per-file content-hash helper
  relocated from ``commands/install.py`` (:pypi:`#762`).
- ``LockfileBuilder`` -- assembles and persists the lockfile from
  :class:`~apm_cli.install.context.InstallContext` state (P2.S6).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.utils.content_hash import compute_file_hash

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.context import InstallContext


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
            try:  # noqa: SIM105
                out[_rel] = compute_file_hash(_full)
            except Exception:
                pass
    return out


class LockfileBuilder:
    """Assembles a ``LockFile`` from :class:`InstallContext` state.

    ``build_and_save()`` is the single entry point -- it creates the
    lockfile from ``ctx.installed_packages``, attaches per-dependency
    metadata, selectively merges entries from a prior lockfile, and
    writes when the semantic content has changed.

    Each ``_attach_*`` / ``_merge_*`` helper mirrors one inline block
    that previously lived inside ``_install_apm_dependencies``; the
    logic is verbatim to preserve behaviour.
    """

    def __init__(self, ctx: InstallContext) -> None:
        self.ctx = ctx

    # -- public API -----------------------------------------------------

    def build_and_save(self) -> None:
        """Assemble lockfile from ctx state and write it (no-op when nothing was installed)."""
        if not self.ctx.installed_packages:
            # Even with nothing newly installed, a pre-existing
            # lockfile may need its cache pin markers refreshed --
            # e.g. user upgraded APM and their cache pre-dates the
            # marker contract. Sync best-effort against the on-disk
            # lockfile.
            self._sync_cache_pin_markers_from_disk()
            return
        try:
            from apm_cli.deps.lockfile import LockFile as _LF
            from apm_cli.deps.lockfile import get_lockfile_path

            lockfile = _LF.from_installed_packages(
                self.ctx.installed_packages, self.ctx.dependency_graph
            )
            # Attach deployed_files and package_type to each LockedDependency
            self._attach_deployed_files(lockfile)
            self._attach_package_types(lockfile)
            # Apply CLI --skill override to lockfile entries (skill_bundle only)
            self._attach_skill_subset_override(lockfile)
            # Attach content hashes captured at download/verify time
            self._attach_content_hashes(lockfile)
            # Attach marketplace provenance if available
            self._attach_marketplace_provenance(lockfile)
            # Selectively merge entries from the existing lockfile:
            #   - For partial installs (only_packages): preserve all old entries
            #     (sequential install -- only the specified package was processed).
            #   - For full installs: only preserve entries for packages still in
            #     the manifest that failed to download (in intended_dep_keys but
            #     not in the new lockfile due to a download error).
            #   - Orphaned entries (not in intended_dep_keys) are intentionally
            #     dropped so the lockfile matches the manifest.
            # Skip merge entirely when update_refs is set -- stale entries must not survive.
            self._merge_existing(lockfile)

            lockfile_path = get_lockfile_path(self.ctx.apm_dir)

            # When installing a subset of packages (apm install <pkg>),
            # merge new entries into the existing lockfile instead of
            # overwriting it -- otherwise the uninstalled packages disappear.
            lockfile = self._maybe_merge_partial(lockfile, lockfile_path, _LF)

            # Only write when the semantic content has actually changed
            # (avoids generated_at churn in version control).
            self._write_if_changed(lockfile, lockfile_path, _LF)
            # Self-heal cache pin markers EVERY install, regardless of
            # whether the lockfile YAML changed. This unblocks users
            # whose caches pre-date the supply-chain hardening (PR
            # #1137 follow-up): if their lockfile is already current,
            # _write_if_changed is a no-op, but markers must still be
            # written so the next `apm audit` drift replay succeeds.
            self._sync_cache_pin_markers(lockfile)
        except Exception as e:
            self._handle_failure(e)

    # -- private helpers (verbatim from original inline block) ----------

    def _attach_deployed_files(self, lockfile: LockFile) -> None:
        for dep_key, dep_files in self.ctx.package_deployed_files.items():
            if dep_key in lockfile.dependencies:
                lockfile.dependencies[dep_key].deployed_files = dep_files
                # Hash the files as they exist on disk AFTER stale
                # cleanup so the recorded hashes match what is now
                # deployed (provenance for the next install's stale
                # cleanup).
                lockfile.dependencies[dep_key].deployed_file_hashes = compute_deployed_hashes(
                    dep_files, self.ctx.project_root
                )

    def _attach_package_types(self, lockfile: LockFile) -> None:
        for dep_key, pkg_type in self.ctx.package_types.items():
            if dep_key in lockfile.dependencies:
                lockfile.dependencies[dep_key].package_type = pkg_type

    def _attach_skill_subset_override(self, lockfile: LockFile) -> None:
        """Apply CLI --skill override to lockfile skill_bundle entries.

        When the user runs `apm install bundle --skill foo`, the CLI
        skill_subset takes precedence over the per-entry skill_subset
        from the manifest for this invocation's lockfile.
        """
        if not self.ctx.skill_subset:
            return  # No CLI override; dep_ref.skill_subset already flows through
        effective = sorted(set(self.ctx.skill_subset))
        for dep_key, locked_dep in lockfile.dependencies.items():  # noqa: B007
            if locked_dep.package_type == "skill_bundle":
                locked_dep.skill_subset = effective

    def _attach_content_hashes(self, lockfile: LockFile) -> None:
        for dep_key, locked_dep in lockfile.dependencies.items():
            if dep_key in self.ctx.package_hashes:
                locked_dep.content_hash = self.ctx.package_hashes[dep_key]

    def _attach_marketplace_provenance(self, lockfile: LockFile) -> None:
        if self.ctx.marketplace_provenance:
            for dep_key, prov in self.ctx.marketplace_provenance.items():
                if dep_key in lockfile.dependencies:
                    lockfile.dependencies[dep_key].discovered_via = prov.get("discovered_via")
                    lockfile.dependencies[dep_key].marketplace_plugin_name = prov.get(
                        "marketplace_plugin_name"
                    )

    def _merge_existing(self, lockfile: LockFile) -> None:
        if self.ctx.existing_lockfile and not self.ctx.update_refs:
            for dep_key, dep in self.ctx.existing_lockfile.dependencies.items():
                if dep_key not in lockfile.dependencies:
                    if self.ctx.only_packages or dep_key in self.ctx.intended_dep_keys:
                        # Preserve: partial install (sequential install support)
                        # OR package still in manifest but failed to download.
                        lockfile.dependencies[dep_key] = dep
                    # else: orphan -- package was in lockfile but is no longer in
                    # the manifest (full install only). Don't preserve so the
                    # lockfile stays in sync with what apm.yml declares.

    def _maybe_merge_partial(self, lockfile: LockFile, lockfile_path: Path, _LF: type) -> LockFile:
        if self.ctx.only_packages:
            existing = _LF.read(lockfile_path)
            if existing:
                for key, dep in lockfile.dependencies.items():  # noqa: B007
                    existing.add_dependency(dep)
                lockfile = existing
        return lockfile

    def _write_if_changed(self, lockfile: LockFile, lockfile_path: Path, _LF: type) -> None:
        # Re-read the on-disk lockfile for the semantic comparison.
        # This is intentionally a FRESH read (not ctx.existing_lockfile)
        # because the partial-install merge above may have modified the
        # in-memory representation.
        existing_lockfile = _LF.read(lockfile_path) if lockfile_path.exists() else None
        if existing_lockfile and lockfile.is_semantically_equivalent(existing_lockfile):
            if self.ctx.logger:
                self.ctx.logger.verbose_detail("apm.lock.yaml unchanged -- skipping write")
        else:
            lockfile.save(lockfile_path)
            if self.ctx.logger:
                self.ctx.logger.verbose_detail(
                    f"Generated apm.lock.yaml with {len(lockfile.dependencies)} dependencies"
                )

    def _handle_failure(self, e: Exception) -> None:
        _lock_msg = f"Could not generate apm.lock.yaml: {e}"
        self.ctx.diagnostics.error(_lock_msg)
        if self.ctx.logger:
            self.ctx.logger.error(_lock_msg)

    def _sync_cache_pin_markers(self, lockfile: LockFile) -> None:
        """Write ``.apm-pin`` markers for every cached remote dep.

        Idempotent and best-effort: a missing or unwritable cache
        directory is silently skipped at the marker-helper level and
        will surface during the next ``apm audit`` drift replay.
        Wrapped in a broad except because lockfile assembly success
        must not be undone by a marker write failure.
        """
        try:
            from apm_cli.install.cache_pin import sync_markers_for_lockfile

            apm_modules_dir = self.ctx.apm_modules_dir
            if apm_modules_dir is None:
                return
            written = sync_markers_for_lockfile(lockfile, self.ctx.project_root, apm_modules_dir)
            if self.ctx.logger and written:
                self.ctx.logger.verbose_detail(
                    f"Wrote {written} cache pin marker(s) for drift replay"
                )
        except Exception as exc:
            if self.ctx.logger:
                self.ctx.logger.verbose_detail(f"Cache pin marker sync skipped: {exc}")

    def _sync_cache_pin_markers_from_disk(self) -> None:
        """Self-heal markers from the on-disk lockfile when no install ran.

        This handles the upgrade path: user installed an older APM,
        runs the new APM with no manifest changes, expects the next
        ``apm audit`` to find every remote dep correctly marked.
        """
        try:
            from apm_cli.deps.lockfile import LockFile as _LF
            from apm_cli.deps.lockfile import get_lockfile_path

            lockfile_path = get_lockfile_path(self.ctx.apm_dir)
            if not lockfile_path.exists():
                return
            lockfile = _LF.load_or_create(lockfile_path)
            self._sync_cache_pin_markers(lockfile)
        except Exception as exc:
            if self.ctx.logger:
                self.ctx.logger.verbose_detail(f"Cache pin marker self-heal skipped: {exc}")

    def compute_deployed_hashes(self, rel_paths) -> dict[str, str]:
        """Delegate to the module-level canonical implementation."""
        return compute_deployed_hashes(rel_paths, self.ctx.project_root)
