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

import copy
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.install.package_resolution import effective_deploy_skill_subset
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
        # Self-heal cache pin markers for remote deps recorded in the
        # PRE-EXISTING on-disk lockfile whose cached payload survives on
        # disk. This runs FIRST, before orphan-pruning (#1926) may rewrite
        # the lockfile to drop deps no longer in the manifest: those deps'
        # cached directories remain under apm_modules/, and the supply-chain
        # contract (PR #1137) requires every cached remote dep to carry a
        # ``.apm-pin`` marker so a later ``apm audit`` drift replay can
        # fail-closed on a stale cache. Idempotent + best-effort; warnings
        # for unpinned deps are surfaced by the primary post-build sync, so
        # this secondary pass stays silent.
        self._sync_cache_pin_markers_from_existing()
        if (
            not self.ctx.installed_packages
            and not self.ctx.lockfile_only
            and not self._has_orphan_lockfile_entries()
        ):
            # Even with nothing newly installed, a pre-existing
            # lockfile may need its cache pin markers refreshed --
            # e.g. user upgraded APM and their cache pre-dates the
            # marker contract. Sync best-effort against the on-disk
            # lockfile.
            #
            # In lockfile_only mode (``apm lock``) we deliberately fall
            # through even with zero dependencies: the command's core
            # promise is to always materialise an ``apm.lock.yaml`` (an
            # empty one for a depless project), mirroring
            # ``cargo generate-lockfile``.
            #
            # We also fall through when the existing lockfile records deps
            # that the manifest no longer declares (orphans): removing every
            # dependency leaves installed_packages empty, but the lockfile
            # must still be pruned to match apm.yml (see
            # ``_has_orphan_lockfile_entries``).
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
            # Attach #1873 executable trust state captured at the gate.
            self._attach_exec_status(lockfile)
            # Apply CLI --skill override to lockfile entries (skill_bundle only)
            self._attach_skill_subset_override(lockfile)
            # Attach content hashes captured at download/verify time
            self._attach_content_hashes(lockfile)
            # Attach declared-license provenance captured at acquire time (U6)
            self._attach_declared_licenses(lockfile)
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
            self._preserve_existing_mcp_state(lockfile)
            self._preserve_existing_lsp_state(lockfile)
            self._preserve_existing_local_state(lockfile)
            self._preserve_existing_revision_pin_tags(lockfile)

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

    def _has_orphan_lockfile_entries(self) -> bool:
        """True when the existing lockfile records apm deps no longer intended.

        Guards the "nothing installed" early-return: when every apm dependency
        is removed from the manifest, ``installed_packages`` is empty, yet the
        lockfile still lists the dropped deps. Falling through lets the normal
        prune path (``_merge_existing``) rebuild a lockfile that matches the
        manifest. Skipped for partial (``only_packages``) installs, which
        intentionally preserve unlisted entries.
        """
        existing = self.ctx.existing_lockfile
        if existing is None or self.ctx.only_packages:
            return False
        from apm_cli.deps.lockfile import _SELF_KEY

        intended = self.ctx.intended_dep_keys or set()
        return any(key != _SELF_KEY and key not in intended for key in existing.dependencies)

    def _reconcile_cross_package_deployed_files(self) -> None:
        """Strip a stale ownership claim when two dep_keys report the same path.

        ``ctx.package_deployed_files`` is populated once per dep_key,
        independently, by that dep's own integration call (see
        ``install/template.py``). When two different packages' primitives
        resolve to the same on-disk path -- a name collision, e.g. two repos
        both shipping a skill called ``shared-topic`` -- each package's own
        integration call correctly and independently reports "I wrote this
        path" at the moment it ran. Under sequential integration
        (``install_order``), a later dep_key's write physically overwrites an
        earlier one's at the same path, so only the last dep_key to claim a
        given path is telling the truth by the time the lockfile is written.

        Without this pass, every dep_key that ever claimed a colliding path
        keeps it in the final lockfile, even though only one of them actually
        owns it on disk -- a lockfile integrity bug: a future
        ``apm uninstall``/``apm audit`` on a "losing" dep would act on a file
        it does not control.
        """
        package_deployed_files = self.ctx.package_deployed_files
        last_owner: dict[str, str] = {}
        for dep_key, files in package_deployed_files.items():
            for f in files:
                last_owner[f] = dep_key  # dict iteration is insertion order -> last write wins
        for dep_key, files in package_deployed_files.items():
            package_deployed_files[dep_key] = [f for f in files if last_owner[f] == dep_key]

    def _attach_deployed_files(self, lockfile: LockFile) -> None:
        """Attach per-dependency deployed-file manifests, unioning targets.

        Reconciliation is **target-scoped**, mirroring the symmetry that
        on-disk stale cleanup already has (``phases/cleanup.py``). Entries a
        prior install recorded for OTHER targets are preserved rather than
        clobbered, so a multi-target deploy keeps every target's files in the
        committed lockfile and they stay covered by the audit gates (issue
        #1716). See :mod:`apm_cli.install.manifest_reconcile`.
        """
        from apm_cli.install.manifest_reconcile import union_preserving
        from apm_cli.install.phases.targets import declared_target_profiles

        self._reconcile_cross_package_deployed_files()
        existing = self.ctx.existing_lockfile
        declared = declared_target_profiles(self.ctx)
        for dep_key, locked_dep in lockfile.dependencies.items():
            current = list(self.ctx.package_deployed_files.get(dep_key, []))
            current_hashes = compute_deployed_hashes(current, self.ctx.project_root)
            prev = existing.get_dependency(dep_key) if existing is not None else None
            prior_files = prev.deployed_files if prev is not None else []
            prior_hashes = prev.deployed_file_hashes if prev is not None else {}

            def _log_ghost_drop(path: str, package_key: str = dep_key) -> None:
                logger = getattr(self.ctx, "logger", None)
                if logger:
                    logger.verbose_detail(
                        f"Dropped inactive-target lockfile entry {path} for {package_key}"
                    )

            files, hashes = union_preserving(
                current,
                current_hashes,
                prior_files,
                prior_hashes,
                self.ctx.targets,
                declared_targets=declared,
                on_ghost_drop=_log_ghost_drop,
            )
            if not files:
                # Nothing this install governs and nothing to carry forward;
                # leave deployed_files untouched so the whole-dep
                # _merge_existing path can preserve it intact.
                continue
            locked_dep.deployed_files = files
            locked_dep.deployed_file_hashes = hashes

    def _attach_package_types(self, lockfile: LockFile) -> None:
        for dep_key, pkg_type in self.ctx.package_types.items():
            if dep_key in lockfile.dependencies:
                lockfile.dependencies[dep_key].package_type = pkg_type

    def _attach_exec_status(self, lockfile: LockFile) -> None:
        """Attach the #1873 ``exec_status`` trust state computed at the gate.

        ``ctx.package_exec_status`` is keyed by dep_key and holds the worst-case
        trust state (``deployed`` / ``gated_pending_approval`` / ``denied``) for
        each dependency that declared executables. Packages with no executables
        are absent from the map and keep ``exec_status=None`` (the audit treats
        them as trusted).
        """
        statuses = getattr(self.ctx, "package_exec_status", None)
        if not statuses:
            return
        for dep_key, status in statuses.items():
            if dep_key in lockfile.dependencies and status:
                lockfile.dependencies[dep_key].exec_status = status

    def _attach_skill_subset_override(self, lockfile: LockFile) -> None:
        """Union the CLI ``--skill`` values into lockfile skill_bundle entries.

        ``--skill`` is additive (issue #1786): a targeted ``apm install bundle
        --skill foo`` must record the UNION of the previously pinned skills and
        ``foo`` -- not replace the persisted subset with the raw CLI value. The
        per-entry manifest subset already flows into ``locked_dep.skill_subset``;
        here we fold in the current CLI values so the lockfile matches what was
        deployed.

        ``--skill '*'`` (empty ``ctx.skill_subset``) is a no-op here -- the
        manifest reset already flowed the full-bundle (empty) subset through.
        """
        if not self.ctx.skill_subset:
            return  # bare install or --skill '*'; manifest value already flows through
        for dep_key, locked_dep in lockfile.dependencies.items():  # noqa: B007
            if locked_dep.package_type == "skill_bundle":
                merged = effective_deploy_skill_subset(
                    skill_subset_from_cli=self.ctx.skill_subset_from_cli,
                    cli_subset=self.ctx.skill_subset,
                    persisted_subset=locked_dep.skill_subset,
                )
                # merged is never None here: the guard above returns for
                # --skill '*' (empty ctx.skill_subset), so a real named
                # subset always survives the union.
                locked_dep.skill_subset = list(merged) if merged else []

    def _attach_content_hashes(self, lockfile: LockFile) -> None:
        for dep_key, locked_dep in lockfile.dependencies.items():
            if dep_key in self.ctx.package_hashes:
                locked_dep.content_hash = self.ctx.package_hashes[dep_key]

    def _attach_declared_licenses(self, lockfile: LockFile) -> None:
        """Attach DECLARED-license provenance captured at acquire time (U6).

        Only deps that actually declared a license appear in
        ``package_declared_licenses``; an absent key leaves ``declared_license``
        as ``None`` so the lockfile OMITS it -- preserving "not declared"
        (unknown) as distinct from an explicit declaration.
        """
        for dep_key, declared in self.ctx.package_declared_licenses.items():
            if dep_key in lockfile.dependencies and declared:
                lockfile.dependencies[dep_key].declared_license = declared

    def _attach_marketplace_provenance(self, lockfile: LockFile) -> None:
        if self.ctx.marketplace_provenance:
            for dep_key, prov in self.ctx.marketplace_provenance.items():
                if dep_key in lockfile.dependencies:
                    lockfile.dependencies[dep_key].discovered_via = prov.get("discovered_via")
                    lockfile.dependencies[dep_key].marketplace_plugin_name = prov.get(
                        "marketplace_plugin_name"
                    )
                    lockfile.dependencies[dep_key].source_url = prov.get("source_url")
                    lockfile.dependencies[dep_key].source_digest = prov.get("source_digest")

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

    def _preserve_existing_mcp_state(self, lockfile: LockFile) -> None:
        """Keep MCP fields until MCPIntegrator reconciles them later in install."""
        if self.ctx.existing_lockfile:
            # MCPIntegrator.update_lockfile runs after this phase and reconciles
            # these carried-forward fields against the current manifest.
            lockfile.mcp_servers = list(self.ctx.existing_lockfile.mcp_servers)
            lockfile.mcp_configs = copy.deepcopy(self.ctx.existing_lockfile.mcp_configs)
            lockfile.mcp_config_provenance = copy.deepcopy(
                self.ctx.existing_lockfile.mcp_config_provenance
            )
            if self.ctx.logger:
                self.ctx.logger.verbose_detail(
                    "MCP state unchanged -- carrying forward "
                    f"{len(lockfile.mcp_servers)} server(s), "
                    f"{len(lockfile.mcp_configs)} config(s)"
                )

    def _preserve_existing_lsp_state(self, lockfile: LockFile) -> None:
        """Keep LSP fields until LSP integration reconciles them later in install."""
        if self.ctx.existing_lockfile:
            lockfile.lsp_servers = list(self.ctx.existing_lockfile.lsp_servers)
            lockfile.lsp_configs = copy.deepcopy(self.ctx.existing_lockfile.lsp_configs)
            if self.ctx.logger:
                self.ctx.logger.verbose_detail(
                    "LSP state unchanged -- carrying forward "
                    f"{len(lockfile.lsp_servers)} server(s), "
                    f"{len(lockfile.lsp_configs)} config(s)"
                )

    def _preserve_existing_local_state(self, lockfile: LockFile) -> None:
        """Keep local fields until post_deps_local reconciles content hashes."""
        if self.ctx.existing_lockfile:
            lockfile.local_deployed_files = list(self.ctx.existing_lockfile.local_deployed_files)
            lockfile.local_deployed_file_hashes = copy.deepcopy(
                self.ctx.existing_lockfile.local_deployed_file_hashes
            )
            if "." in self.ctx.existing_lockfile.dependencies:
                lockfile.dependencies["."] = copy.deepcopy(
                    self.ctx.existing_lockfile.dependencies["."]
                )
            if self.ctx.logger:
                self.ctx.logger.verbose_detail(
                    "Carrying forward local .apm state pending hash reconciliation: "
                    f"{len(lockfile.local_deployed_files)} file(s)"
                )

    def _preserve_existing_revision_pin_tags(self, lockfile: LockFile) -> None:
        """Carry resolved_tag for unchanged SHA-pinned deps across installs."""
        existing = self.ctx.existing_lockfile
        if not existing:
            return
        for key, dep in lockfile.dependencies.items():
            if dep.resolved_tag:
                continue
            prev = existing.get_dependency(key)
            if prev is None or not prev.resolved_tag:
                continue
            if (
                dep.resolved_ref == prev.resolved_ref
                and dep.resolved_commit == prev.resolved_commit
            ):
                dep.resolved_tag = prev.resolved_tag

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

    def _sync_cache_pin_markers_from_existing(self) -> None:
        """Self-heal markers from the pre-existing in-memory lockfile.

        Runs at the very start of ``build_and_save`` so that remote deps
        recorded in the on-disk lockfile get their ``.apm-pin`` markers
        even when the imminent build prunes them (orphan removal, #1926):
        the pruned dep's cache remains under ``apm_modules/`` and the
        supply-chain contract (#1137) requires every cached remote dep to
        be marked. Warnings are suppressed here -- the primary post-build
        sync over the freshly-built lockfile surfaces unpinned deps, so a
        second warning pass would be noise. Best-effort: a marker write
        failure must never abort the install.
        """
        existing = self.ctx.existing_lockfile
        if existing is None:
            return
        try:
            from apm_cli.install.cache_pin import sync_markers_for_lockfile

            apm_modules_dir = self.ctx.apm_modules_dir
            if apm_modules_dir is None:
                return
            written = sync_markers_for_lockfile(
                existing,
                self.ctx.project_root,
                apm_modules_dir,
                warn_unpinned=False,
            )
            if self.ctx.logger and written:
                self.ctx.logger.verbose_detail(
                    f"Self-healed {written} cache pin marker(s) from existing lockfile"
                )
        except Exception as exc:
            if self.ctx.logger:
                self.ctx.logger.verbose_detail(
                    f"Cache pin marker self-heal (existing) skipped: {exc}"
                )

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
