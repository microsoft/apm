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

from apm_cli.core.scope import is_user_scope
from apm_cli.install.package_resolution import effective_dependency_selection, is_implicit_local
from apm_cli.install.phases.lockfile_cache_pins import (
    sync_cache_pin_markers,
    sync_cache_pin_markers_from_disk,
    sync_cache_pin_markers_from_existing,
)
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
    contribute meaningful provenance). ``cowork://`` rows hash the file
    they decode to under the resolved Cowork root, so they carry the same
    provenance as project rows (#3701); without a resolvable root they get
    no hash.
    """
    from apm_cli.integration.copilot_cowork_paths import (
        COWORK_URI_SCHEME,
        CoworkResolutionError,
        from_lockfile_path,
        resolve_copilot_cowork_skills_dir,
    )
    from apm_cli.utils.path_security import PathTraversalError

    out: dict = {}
    cowork_root: Path | None = None
    cowork_resolved = False
    for _rel in rel_paths or ():
        if _rel.startswith(COWORK_URI_SCHEME):
            if not cowork_resolved:
                cowork_resolved = True
                try:
                    cowork_root = resolve_copilot_cowork_skills_dir()
                except CoworkResolutionError:
                    cowork_root = None
            if cowork_root is None:
                continue
            try:
                _full = from_lockfile_path(_rel, cowork_root)
            except (PathTraversalError, ValueError):
                continue
        else:
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

    def save_replacement_intent(self) -> None:
        """Save metadata-only intent, retaining every existing deployment receipt."""
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        if not self.ctx.lockfile_only and not self._replacement_table_changed():
            return
        existing = self.ctx.existing_lockfile
        lockfile = copy.deepcopy(existing) if existing is not None else LockFile()
        self._attach_replacement_table(lockfile)
        self._write_if_changed(lockfile, get_lockfile_path(self.ctx.apm_dir), LockFile)

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
        sync_cache_pin_markers_from_existing(self.ctx)
        # Reconcile dropped-target merge-hook JSON/sidecar state (issue
        # #2253) unconditionally, before the early-return below. Whether
        # this cleanup is needed depends purely on "did the declared
        # target set shrink", not on whether this run installed or
        # orphaned any package -- coupling it to `installed_packages`
        # would miss narrow+remove-all-deps shapes that reach the
        # early-return below with nothing installed. Guarded only by
        # `lockfile_only` (`apm lock` never touches deployed state).
        # Structurally unreachable under `--dry-run`: `apm install
        # --dry-run` never calls `build_and_save()` at all (see the
        # separate `render_and_exit` preview path in `commands/install.py`).
        if not self.ctx.lockfile_only:
            self._reconcile_dropped_merge_hook_targets()
        if (
            not self.ctx.installed_packages
            and not self.ctx.lockfile_only
            and not self._has_orphan_lockfile_entries()
            and not self._replacement_table_changed()
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
            self._reconcile_target_deployed_files(self.ctx.existing_lockfile)
            sync_cache_pin_markers_from_disk(self.ctx)
            return
        try:
            from apm_cli.deps.lockfile import LockFile as _LF
            from apm_cli.deps.lockfile import get_lockfile_path, records_framed_content_hashes
            from apm_cli.install.phases.content_hash_stream import (
                PROMOTED_MESSAGE,
                promote_content_hashes,
            )

            lockfile = _LF.from_installed_packages(
                self.ctx.installed_packages, self.ctx.dependency_graph
            )
            # Rows carry the stream this run records in until promotion settles it.
            lockfile.set_content_hash_stream(
                framed=records_framed_content_hashes(self.ctx.existing_lockfile)
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
            # Intact native plugins deployed for their dependency (#3191); after
            # content hashes, which decide whether an existing record carries over.
            self._attach_native_plugins(lockfile)
            # Attach declared-license provenance captured at acquire time (U6)
            self._attach_declared_licenses(lockfile)
            # Attach verbatim licence copies carried at materialization (#3556)
            self._attach_carried_licenses(lockfile)
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
            self._retire_undeclared_native_plugins(lockfile)
            self._attach_replacement_table(lockfile)
            # Restore local compatibility state first so the canonical ledger is
            # complete when MCP target rows are projected into it.
            self._preserve_existing_local_state(lockfile)
            self._preserve_existing_mcp_state(lockfile)
            self._preserve_existing_lsp_state(lockfile)
            self._preserve_existing_revision_pin_tags(lockfile)
            # After every carry-forward step, so promotion sees each final row.
            promoted = promote_content_hashes(self.ctx, lockfile)
            # The carry-forward steps above set fields such as resolved_tag
            # and replacements directly, so re-derive the schema version
            # before the semantic comparison (#3433).
            lockfile.sync_lockfile_version()

            # Only write when the semantic content has actually changed
            # (avoids generated_at churn in version control).
            self._write_if_changed(lockfile, lockfile_path, _LF)
            if promoted and self.ctx.logger:
                self.ctx.logger.info(PROMOTED_MESSAGE)
            self._report_new_content(lockfile)
            # Target-scoped deployed-file contraction physically deletes the
            # dropped target's instruction files (via the cleanup chokepoint).
            # Guard it by `lockfile_only` for the same reason the merge-hook
            # reconciliation above is guarded: `apm lock` never touches
            # deployed state -- it only (re)materialises the lockfile YAML.
            if not self.ctx.lockfile_only:
                self._reconcile_target_deployed_files(lockfile)
            # Self-heal cache pin markers EVERY install, regardless of
            # whether the lockfile YAML changed. This unblocks users
            # whose caches pre-date the supply-chain hardening (PR
            # #1137 follow-up): if their lockfile is already current,
            # _write_if_changed is a no-op, but markers must still be
            # written so the next `apm audit` drift replay succeeds.
            sync_cache_pin_markers(self.ctx, lockfile)
        except Exception as e:
            self._handle_failure(e)

    # -- private helpers (verbatim from original inline block) ----------

    def _replacement_table_changed(self) -> bool:
        """Keep dormant rule changes out of the no-packages early return."""
        from apm_cli.install.plan import lockfile_satisfies_replacements

        rules = getattr(getattr(self.ctx, "apm_package", None), "replace_rules", None)
        satisfied, _ = lockfile_satisfies_replacements(self.ctx.existing_lockfile, rules)
        return not satisfied

    def _attach_replacement_table(self, lockfile: LockFile) -> None:
        """Persist all root rules, including unused and explicitly empty tables."""
        from apm_cli.models.dependency.replacement import replacement_rule_to_dict

        rules = getattr(getattr(self.ctx, "apm_package", None), "replace_rules", None)
        lockfile.replacements = (
            None if rules is None else [replacement_rule_to_dict(rule) for rule in rules]
        )

    def _has_orphan_lockfile_entries(self) -> bool:
        """True when the existing lockfile records apm deps no longer intended.

        Guards the "nothing installed" early-return: when every apm dependency
        is removed from the manifest, ``installed_packages`` is empty, yet the
        lockfile still lists the dropped deps. Falling through lets the normal
        prune path (``_merge_existing``) rebuild a lockfile that matches the
        manifest. Skipped for partial (``only_packages``) installs, which
        intentionally preserve unlisted entries.
        """
        if self.ctx.existing_lockfile is None or self.ctx.only_packages:
            return False
        return bool(self._orphan_decision().candidate_keys)

    def _orphan_decision(self):
        """Ask the orphan owner which previous rows a full install retires."""
        from apm_cli.install.orphans import (
            DeploymentClaims,
            OrphanOperation,
            decide_orphan_removal,
        )

        return decide_orphan_removal(
            operation=OrphanOperation.INSTALL,
            roots=(),
            candidate_universe=self.ctx.existing_lockfile,
            installed_paths=None,
            resolved_keys=None,
            intended=self.ctx.intended_dep_keys or set(),
            materialized=None,
            reachability=None,
            deployment_claims=DeploymentClaims(
                live_files=frozenset(),
                retained=getattr(self.ctx, "orphan_cleanup_retained", {}),
            ),
        )

    def _attach_deployed_files(self, lockfile: LockFile) -> None:
        """Attach per-dependency deployed-file manifests, unioning targets.

        Reconciliation is **target-scoped**, mirroring the symmetry that
        on-disk stale cleanup already has (``phases/cleanup.py``). Entries a
        prior install recorded for OTHER targets are preserved rather than
        clobbered, so a multi-target deploy keeps every target's files in the
        committed lockfile and they stay covered by the audit gates (issue
        #1716). See :mod:`apm_cli.install.manifest_reconcile`.
        """
        from apm_cli.install.manifest_reconcile import reconcile_deployed_block
        from apm_cli.install.phases.cleanup import deployment_evidence, kept_prior_values
        from apm_cli.install.phases.targets import declared_target_profiles

        existing = self.ctx.existing_lockfile
        prior_ledger = None
        if existing is not None:
            from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

            prior_ledger = DeploymentLedgerCodec.from_lockfile(existing)
        prior_files_by_package: dict[str, list[str]] = {}
        prior_hashes_by_package: dict[str, dict[str, str]] = {}
        receipted = getattr(self.ctx, "receipted_prior_deployments", {})
        for dep_key in lockfile.dependencies:
            previous = existing.get_dependency(dep_key) if existing is not None else None
            stand_in = receipted.get(dep_key, {})
            prior_files_by_package[dep_key] = (
                previous.deployed_files if previous is not None else sorted(stand_in)
            )
            prior_hashes_by_package[dep_key] = (
                previous.deployed_file_hashes if previous is not None else dict(stand_in)
            )
        from apm_cli.core.deployment_state import DeploymentReconciler

        package_claims = DeploymentReconciler.reconcile_package_claims(
            package_keys=lockfile.dependencies,
            current_claims=self.ctx.package_deployed_files,
            prior_files=prior_files_by_package,
            prior_hashes=prior_hashes_by_package,
        )
        declared = declared_target_profiles(self.ctx)
        diagnostics = getattr(self.ctx, "diagnostics", None)
        if diagnostics is None:
            from apm_cli.utils.diagnostics import DiagnosticCollector

            diagnostics = DiagnosticCollector()
        cleanup_retained = getattr(self.ctx, "package_cleanup_retained", {})
        from apm_cli.core.deployment_state import DeploymentLedger

        # `apm lock` (lockfile_only) reconciles the lockfile without touching
        # the working tree: dropped-target files must stay on disk, with the
        # physical prune deferred to the next `apm install` (issue #2296). In
        # that mode we also skip ghost-drop accounting -- rows are preserved,
        # not removed, so a "Repaired inactive-target entries" report would be
        # misleading.
        lockfile_only = getattr(self.ctx, "lockfile_only", False)
        from apm_cli.install.service import InstallService

        frozen_disposition = InstallService.frozen_disposition(self.ctx)
        completed_reintegrations = (
            {}
            if lockfile_only or frozen_disposition.withhold_lockfile_writes
            else getattr(self.ctx, "package_completed_reintegrations", {})
        )
        # A kept native plugin deployed nothing this run: its previous ledger
        # records stand, never re-attributed to this run (#3921). Seeded first,
        # so a dependency this run evaluated wins any record it co-owns.
        carried_keys = getattr(self.ctx, "native_carried_keys", None) or set()
        canonical_records = {
            key: record
            for key, record in (prior_ledger.records.items() if prior_ledger else ())
            if carried_keys & set(record.owners)
        }
        authoritative_empty = False
        ghost_count = 0
        for dep_key in lockfile.dependencies:
            if dep_key in carried_keys:
                continue
            claim = package_claims[dep_key]
            current = list(claim.current_files)
            retained_hashes = cleanup_retained.get(dep_key, {})
            current_hashes = compute_deployed_hashes(
                (path for path in current if path not in retained_hashes),
                self.ctx.project_root,
            )
            prior_files = list(claim.prior_files)
            prior_hashes = claim.prior_hashes
            completed = completed_reintegrations.get(dep_key)

            def _log_ghost_drop(path: str, package_key: str = dep_key) -> None:
                nonlocal ghost_count
                ghost_count += 1
                logger = getattr(self.ctx, "logger", None)
                if logger:
                    logger.verbose_detail(
                        f"Removed stale lockfile path {path} "
                        f"(target not declared in apm.yml) for {package_key}"
                    )

            files, _hashes, ledger = reconcile_deployed_block(
                project_root=self.ctx.project_root,
                dep_key=dep_key,
                current_files=current,
                current_hashes=current_hashes,
                prior_files=prior_files,
                prior_hashes=prior_hashes,
                active_targets=self.ctx.targets,
                declared_targets=declared,
                diagnostics=diagnostics,
                evidence=deployment_evidence(self.ctx),
                on_ghost_drop=None if lockfile_only else _log_ghost_drop,
                prior_ledger=prior_ledger,
                cleanup_retained_hashes=retained_hashes,
                current_run_trusted=diagnostics.count_for_package(dep_key, "error") == 0,
                owner=dep_key,
                include_ledger=True,
                apply_disk_deletion=not lockfile_only,
                user_scope=is_user_scope(getattr(self.ctx, "scope", None)),
                completed_reintegration=completed,
                retained_values=frozenset(kept_prior_values(self.ctx, prior_files)),
            )
            if not files and completed is None:
                # Nothing this install governs and nothing to carry forward;
                # leave deployed_files untouched so the whole-dep
                # _merge_existing path can preserve it intact.
                continue
            canonical_records.update(ledger.records)
            authoritative_empty |= completed is not None and not files
        if canonical_records or authoritative_empty:
            from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

            DeploymentLedgerCodec.apply_to_lockfile(
                DeploymentLedger(records=canonical_records),
                lockfile,
            )
        logger = getattr(self.ctx, "logger", None)
        if logger and ghost_count:
            noun = "entry" if ghost_count == 1 else "entries"
            logger.info(f"Repaired {ghost_count} inactive-target lockfile {noun}")

    def _attach_package_types(self, lockfile: LockFile) -> None:
        for dep_key, pkg_type in self.ctx.package_types.items():
            if dep_key in lockfile.dependencies:
                lockfile.dependencies[dep_key].package_type = pkg_type
        for dep_key, targets in self.ctx.package_targets.items():
            if dep_key in lockfile.dependencies:
                lockfile.dependencies[dep_key].package_targets = list(targets)

    def _attach_exec_status(self, lockfile: LockFile) -> None:
        """Attach the #1873 ``exec_status`` trust state computed at the gate.

        ``ctx.package_exec_status`` is keyed by dep_key and holds the worst-case
        trust state (``deployed`` / ``gated_pending_approval`` / ``denied``) for
        each dependency that declared executables. Packages with no executables
        are absent from the map and keep ``exec_status=None`` (the audit treats
        them as trusted).

        ``apm lock`` deploys nothing, so the gate never runs; each row keeps
        the trust state recorded for its deployed files, as those files are
        carried forward too (#4213).
        """
        statuses = getattr(self.ctx, "package_exec_status", None) or {}
        allowed = getattr(self.ctx, "package_exec_allowed_types", None) or {}
        for dep_key, status in statuses.items():
            if dep_key in lockfile.dependencies and status:
                lockfile.dependencies[dep_key].exec_status = status
                lockfile.dependencies[dep_key].exec_allowed_types = allowed.get(dep_key, ())
        existing = getattr(self.ctx, "existing_lockfile", None)
        if not getattr(self.ctx, "lockfile_only", False) or existing is None:
            return
        for dep_key, dep in lockfile.dependencies.items():
            prior = existing.get_dependency(dep_key)
            if dep_key not in statuses and prior is not None and prior.exec_status:
                dep.exec_status = prior.exec_status
                dep.exec_allowed_types = prior.exec_allowed_types

    def _attach_native_plugins(self, lockfile: LockFile) -> None:
        """Record each admitted native plugin as its dependency's ownership.

        A run whose targets exclude every harness a recorded plugin loads in
        did not evaluate its admission, so that record is carried forward
        instead of retiring the plugin.
        """
        from apm_cli.claude_plugins.registrar import native_ownership, scope_binding
        from apm_cli.core.scope import is_user_scope
        from apm_cli.native_plugins.record import NativePluginRecord

        plugins = getattr(self.ctx, "claude_native_plugins", None) or {}
        existing = getattr(self.ctx, "existing_lockfile", None)
        if plugins:
            name, path = scope_binding(
                user_scope=is_user_scope(self.ctx.scope),
                modules_dir=self.ctx.apm_modules_dir,
                deploy_root=self.ctx.project_root,
                previous=None if existing is None else native_ownership(existing),
            )
        for dep_key, plugin in plugins.items():
            if dep_key in lockfile.dependencies:
                lockfile.dependencies[dep_key].native_plugin = NativePluginRecord(
                    plugin.name, plugin.bundle_hash, name, path
                )
        if existing is None:
            return
        from apm_cli.native_plugins.carry import carry_unevaluated_native_plugins

        carry_unevaluated_native_plugins(self.ctx, lockfile, existing)

    def _retire_undeclared_native_plugins(self, lockfile: LockFile) -> None:
        """Drop native records once contraction retires every harness that loads them (#4744).

        The target-contraction rule (``target_governance.target_retired``):
        apm.yml declares targets, and neither they nor this run's targets name
        claude. Applied to the merged lock, so a partial install retires
        untouched plugins too; native registration then removes their roots
        and Claude declarations. ``apm lock`` touches no deployed state, so it
        keeps them.
        """
        from apm_cli.install.phases.targets import declared_target_profiles
        from apm_cli.install.target_governance import retired_native_plugins

        if self.ctx.lockfile_only:
            return
        declared = declared_target_profiles(self.ctx)
        for key in retired_native_plugins(lockfile, self.ctx.targets or (), declared):
            lockfile.dependencies[key].native_plugin = None

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
            if locked_dep.package_type == "skill_bundle" and not is_implicit_local(
                locked_dep, self.ctx
            ):
                # Record what was deployed: the same owner the deploy used (#3760).
                merged = effective_dependency_selection(locked_dep, self.ctx).skill_subset
                try:
                    pairing = locked_dep.selection()
                except ValueError:
                    pairing = None
                # merged is never None here: the guard above returns for
                # --skill '*' (empty ctx.skill_subset), so a real named
                # subset always survives the union.
                locked_dep.skill_subset = list(merged) if merged else []
                if pairing is not None and not pairing.is_product:
                    # The CLI overlay is one more edge over the entry's targets
                    # (req-rs-018), as effective_dependency_selection deploys it.
                    pairing = pairing.with_edge(self.ctx.skill_subset, locked_dep.target_subset)
                    locked_dep.subset_pairings = pairing.to_lock_records()
                else:
                    locked_dep.subset_pairings = None  # the widened flat product is exact

    def _attach_content_hashes(self, lockfile: LockFile) -> None:
        for dep_key, locked_dep in lockfile.dependencies.items():
            if dep_key in self.ctx.package_hashes:
                locked_dep.content_hash = self.ctx.package_hashes[dep_key]

    def _report_new_content(self, lockfile: LockFile) -> None:
        """Name the dependencies whose locked content changed this run.

        Only a moved commit (or a row without one) may change content (#4027).
        Runs after promotion and the write, so it reports the stored hashes.
        """
        from apm_cli.install.source_helpers import locked_commit_unchanged

        previous = getattr(self.ctx, "existing_lockfile", None)
        logger = getattr(self.ctx, "logger", None)
        if previous is None or logger is None:
            return
        changed = []
        for dep_key, locked_dep in lockfile.dependencies.items():
            old = previous.dependencies.get(dep_key)
            if (
                old is not None
                and old.content_hash
                and locked_dep.content_hash
                and old.content_hash != locked_dep.content_hash
                and not locked_commit_unchanged(old, locked_dep.resolved_commit)
            ):
                changed.append((dep_key, locked_dep))
        if not changed:
            return
        noun = "dependency" if len(changed) == 1 else "dependencies"
        logger.info(f"New locked content for {len(changed)} {noun} (see apm.lock.yaml)")
        for dep_key, locked_dep in changed:
            commit = (locked_dep.resolved_commit or "no commit")[:12]
            logger.verbose_detail(f"  {dep_key}: {locked_dep.content_hash} ({commit})")

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

    def _attach_carried_licenses(self, lockfile: LockFile) -> None:
        """Attach the licence copies carried into each materialized package (#3556)."""
        for dep_key, carried in self.ctx.package_carried_licenses.items():
            if dep_key in lockfile.dependencies:
                lockfile.dependencies[dep_key].carried_licenses = tuple(carried)

    def _attach_marketplace_provenance(self, lockfile: LockFile) -> None:
        # Canonical manifest entries do not rediscover their marketplace on
        # update. Keep the original discovery snapshot for surviving identities
        # without carrying forward stale commits, hashes, or removed entries.
        if self.ctx.existing_lockfile:
            for dep_key, dep in lockfile.dependencies.items():
                previous = self.ctx.existing_lockfile.dependencies.get(dep_key)
                # Ports are not part of the dependency key.
                if previous is not None and previous.port == dep.port:
                    dep.discovered_via = previous.discovered_via
                    dep.marketplace_plugin_name = previous.marketplace_plugin_name
                    dep.source_url = previous.source_url
                    dep.source_digest = previous.source_digest
                    if (
                        previous.constraint
                        and previous.resolved_tag
                        and previous.resolved_ref == dep.resolved_ref
                        and previous.resolved_commit == dep.resolved_commit
                        and not dep.constraint
                    ):
                        dep.constraint = previous.constraint
                        dep.resolved_tag = previous.resolved_tag
                        dep.version = previous.version
                        dep.resolved_at = previous.resolved_at

        # Fresh discovery replaces the entire snapshot, including absent fields.
        if self.ctx.marketplace_provenance:
            for dep_key, prov in self.ctx.marketplace_provenance.items():
                if dep_key in lockfile.dependencies:
                    dep = lockfile.dependencies[dep_key]
                    dep.discovered_via = prov.get("discovered_via")
                    dep.marketplace_plugin_name = prov.get("marketplace_plugin_name")
                    dep.source_url = prov.get("source_url")
                    dep.source_digest = prov.get("source_digest")
                    if prov.get("constraint"):
                        dep.constraint = prov.get("constraint")
                    if prov.get("resolved_tag"):
                        dep.resolved_tag = prov.get("resolved_tag")
                    if prov.get("resolved_version"):
                        dep.version = prov["resolved_version"]
                    if prov.get("resolved_at"):
                        dep.resolved_at = prov["resolved_at"]
                        previous = (
                            self.ctx.existing_lockfile.dependencies.get(dep_key)
                            if self.ctx.existing_lockfile
                            else None
                        )
                        if (
                            previous is not None
                            and previous.resolved_at
                            and dep.resolved_commit
                            and all(
                                getattr(previous, field) == getattr(dep, field)
                                for field in (
                                    "port",
                                    "source",
                                    "host_type",
                                    "resolved_commit",
                                    "resolved_ref",
                                    "constraint",
                                    "resolved_tag",
                                    "version",
                                    "discovered_via",
                                    "marketplace_plugin_name",
                                    "source_url",
                                    "source_digest",
                                )
                            )
                        ):
                            dep.resolved_at = previous.resolved_at

    def _merge_existing(self, lockfile: LockFile) -> None:
        # Partial operations merge untouched dependencies and their canonical
        # deployment records together in _maybe_merge_partial.
        if self.ctx.only_packages:
            return
        if self.ctx.existing_lockfile and not self.ctx.update_refs:
            retained_orphans = getattr(self.ctx, "orphan_cleanup_retained", {})
            decision = self._orphan_decision()
            for dep_key, dep in self.ctx.existing_lockfile.dependencies.items():
                if dep_key not in lockfile.dependencies:
                    if dep_key in decision.rescued_keys:
                        # Preserve: package still in manifest but failed to download.
                        lockfile.dependencies[dep_key] = dep
                    elif dep_key in decision.retained_keys and (
                        retained := retained_orphans.get(dep_key)
                    ):
                        self._keep_retained_orphan(
                            lockfile, dep_key, dep, retained, self.ctx.existing_lockfile
                        )
                    # else: orphan -- package was in lockfile but is no longer in
                    # the manifest (full install only). Don't preserve so the
                    # lockfile stays in sync with what apm.yml declares.

    @staticmethod
    def _keep_retained_orphan(
        lockfile: LockFile,
        dep_key: str,
        dep: object,
        retained: dict[str, str | None],
        source: LockFile | None = None,
    ) -> None:
        """Keep an orphan's undeleted files owned so the next install can retry.

        *source* is the lockfile the orphan came from when *lockfile* is a fresh
        build; its rows keep the orphan's concrete target attribution.
        """
        retained_files = [path for path in dep.deployed_files if path in retained]
        retained_hashes = {
            path: dep.deployed_file_hashes[path]
            for path in retained_files
            if path in dep.deployed_file_hashes
        }
        lockfile.dependencies[dep_key] = copy.deepcopy(dep)
        from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

        DeploymentLedgerCodec.replace_legacy_owner(
            lockfile,
            dep_key,
            retained_files,
            retained_hashes,
            provenance=DeploymentLedgerCodec.from_lockfile(source) if source else None,
        )

    def _reconcile_partial_subtree(self, existing: LockFile) -> None:
        """Retire the selected packages' dropped descendants from a partial merge (#3502).

        Pinned entries -- undeleted files or their dropped ancestors -- stay
        locked with only their retained files so the next install can retry.
        Rescued descendants -- still reached through another retained
        package -- keep their entry but take their current parent and local
        declaration from the complete graph so later subtree scans, ``apm
        deps tree`` and local-source anchoring agree.
        """
        retained_orphans = getattr(self.ctx, "orphan_cleanup_retained", {})
        pinned = getattr(self.ctx, "partial_pinned_dep_keys", frozenset())
        for dep_key in sorted(getattr(self.ctx, "partial_dropped_dep_keys", ())):
            dep = existing.dependencies.pop(dep_key, None)
            if dep is not None and dep_key in pinned:
                self._keep_retained_orphan(
                    existing, dep_key, dep, retained_orphans.get(dep_key, {})
                )
        graph = getattr(self.ctx, "dependency_graph", None)
        if graph is None:
            return
        from apm_cli.install.source_helpers import locked_parent_identity

        for dep_key in sorted(getattr(self.ctx, "partial_rescued_dep_keys", ())):
            dep = existing.dependencies.get(dep_key)
            node = graph.dependency_tree.get_node(dep_key)
            if dep is None or node is None or dep.replacement is not None:
                continue
            dep.resolved_by = locked_parent_identity(self.ctx, node)
            dep.depth = node.depth
            reference = node.dependency_ref
            if reference.is_local:
                dep.local_path = reference.local_path
                dep.declaring_parent = reference.declaring_parent
                dep.anchored_local_path = reference.anchored_local_path

    def _maybe_merge_partial(self, lockfile: LockFile, lockfile_path: Path, _LF: type) -> LockFile:
        if self.ctx.only_packages:
            existing = _LF.read(lockfile_path)
            if existing:
                from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

                self._reconcile_partial_subtree(existing)
                DeploymentLedgerCodec.merge_dependencies(
                    existing,
                    lockfile,
                    project_root=self.ctx.project_root,
                    diagnostics=self.ctx.diagnostics,
                )
                lockfile = existing
        return lockfile

    def _preserve_existing_mcp_state(self, lockfile: LockFile) -> None:
        """Keep MCP fields until MCPIntegrator reconciles them later in install."""
        if self.ctx.existing_lockfile:
            # MCPIntegrator.update_lockfile runs after this phase and reconciles
            # these carried-forward fields against the current manifest.
            lockfile.mcp_servers = list(self.ctx.existing_lockfile.mcp_servers)
            lockfile.mcp_configs = copy.deepcopy(self.ctx.existing_lockfile.mcp_configs)
            lockfile.mcp_omissions = copy.deepcopy(self.ctx.existing_lockfile.mcp_omissions)
            lockfile.mcp_config_provenance = copy.deepcopy(
                self.ctx.existing_lockfile.mcp_config_provenance
            )
            target_servers = self.ctx.existing_lockfile.mcp_target_servers
            from apm_cli.install.mcp.retired_destinations import (
                lock_holds_untranslated_mcp_claims,
            )

            # Until the MCP phase translates them, these claims mean what the
            # recorded version meant; restamping would make the heal skip them.
            if lock_holds_untranslated_mcp_claims(
                self.ctx.existing_lockfile.apm_version, target_servers
            ):
                lockfile.apm_version = self.ctx.existing_lockfile.apm_version
            # The codec rebuild is linear in deployment rows and also marks the
            # compatibility view present, so skip it when there is no target
            # state to preserve.
            if target_servers:
                from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

                DeploymentLedgerCodec.replace_mcp_target_servers(
                    lockfile,
                    copy.deepcopy(target_servers),
                    native_hashes=DeploymentLedgerCodec.mcp_native_hashes(
                        self.ctx.existing_lockfile
                    ),
                )
            if self.ctx.logger:
                server_count = len(lockfile.mcp_servers)
                config_count = len(lockfile.mcp_configs)
                server_noun = "server" if server_count == 1 else "servers"
                config_noun = "config" if config_count == 1 else "configs"
                detail = (
                    "MCP state unchanged -- carrying forward "
                    f"{server_count} {server_noun}, "
                    f"{config_count} {config_noun}"
                )
                if target_servers:
                    mapping_count = len(target_servers)
                    mapping_noun = "mapping" if mapping_count == 1 else "mappings"
                    detail += f", {mapping_count} target {mapping_noun}"
                self.ctx.logger.verbose_detail(detail)

    def _preserve_existing_lsp_state(self, lockfile: LockFile) -> None:
        """Keep LSP fields until LSP integration reconciles them later in install."""
        if self.ctx.existing_lockfile:
            lockfile.lsp_servers = list(self.ctx.existing_lockfile.lsp_servers)
            lockfile.lsp_configs = copy.deepcopy(self.ctx.existing_lockfile.lsp_configs)
            lockfile.lsp_config_provenance = copy.deepcopy(
                self.ctx.existing_lockfile.lsp_config_provenance
            )
            target_servers = self.ctx.existing_lockfile.lsp_target_servers
            if target_servers:
                from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

                DeploymentLedgerCodec.replace_lsp_target_servers(
                    lockfile,
                    copy.deepcopy(target_servers),
                )
            if self.ctx.logger:
                self.ctx.logger.verbose_detail(
                    "LSP state unchanged -- carrying forward "
                    f"{len(lockfile.lsp_servers)} server(s), "
                    f"{len(lockfile.lsp_configs)} config(s)"
                )

    def _preserve_existing_local_state(self, lockfile: LockFile) -> None:
        """Keep local fields until post_deps_local reconciles content hashes."""
        if self.ctx.existing_lockfile:
            from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

            DeploymentLedgerCodec.replace_legacy_owner(
                lockfile,
                ".",
                list(self.ctx.existing_lockfile.local_deployed_files),
                copy.deepcopy(self.ctx.existing_lockfile.local_deployed_file_hashes),
                provenance=DeploymentLedgerCodec.from_lockfile(self.ctx.existing_lockfile),
                current_targets=self.ctx.targets or (),
            )
            lockfile.carry_local_staging_from(self.ctx.existing_lockfile)
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
            persisted = lockfile.save(lockfile_path, existing_lockfile=existing_lockfile)
            if persisted and self.ctx.logger:
                self.ctx.logger.record_lockfile_write()
                self.ctx.logger.verbose_detail(
                    f"Generated apm.lock.yaml with {len(lockfile.dependencies)} dependencies"
                )

    def _handle_failure(self, e: Exception) -> None:
        _lock_msg = f"Could not generate apm.lock.yaml: {e}"
        self.ctx.diagnostics.error(_lock_msg)
        if self.ctx.logger:
            self.ctx.logger.error(_lock_msg)

    def _reconcile_dropped_merge_hook_targets(self) -> None:
        """Clean merge-hook JSON/sidecar state for targets dropped from ``targets:``.

        Best-effort, matching ``sync_cache_pin_markers_from_existing``'s own
        convention: a failure here must never abort the install. Internal
        malformed-state handling already fails closed and warns (see
        ``HookIntegrator.reconcile_dropped_targets``); this wrapper only
        guards against an unexpected exception escaping that call.
        """
        try:
            from apm_cli.core.scope import InstallScope
            from apm_cli.install.manifest_reconcile import (
                reconcile_dropped_merge_hook_targets,
            )
            from apm_cli.install.phases.targets import declared_target_profiles

            declared = declared_target_profiles(self.ctx)
            is_user = getattr(self.ctx, "scope", None) is InstallScope.USER
            reconcile_dropped_merge_hook_targets(
                self.ctx.project_root,
                active_targets=self.ctx.targets,
                declared_targets=declared,
                user_scope=is_user,
            )
        except Exception as exc:
            if self.ctx.logger:
                self.ctx.logger.verbose_detail(f"Dropped-target hook reconciliation skipped: {exc}")

    def _reconcile_target_deployed_files(self, lockfile: LockFile | None) -> None:
        """Apply the canonical file contraction owner to the final lockfile."""
        if lockfile is None:
            return
        from apm_cli.core.scope import InstallScope
        from apm_cli.deps.lockfile import get_lockfile_path
        from apm_cli.install.manifest_reconcile import reconcile_target_deployed_files
        from apm_cli.install.phases.cleanup import deployment_evidence
        from apm_cli.install.phases.targets import declared_target_profiles

        declared = declared_target_profiles(self.ctx)
        is_user = getattr(self.ctx, "scope", None) is InstallScope.USER
        changed = reconcile_target_deployed_files(
            project_root=self.ctx.project_root,
            lockfile=lockfile,
            active_targets=self.ctx.targets,
            declared_targets=declared,
            diagnostics=self.ctx.diagnostics,
            evidence=deployment_evidence(self.ctx),
            user_scope=is_user,
            logger=getattr(self.ctx, "logger", None),
        )
        if changed:
            lockfile.save(get_lockfile_path(self.ctx.apm_dir))

    def compute_deployed_hashes(self, rel_paths) -> dict[str, str]:
        """Delegate to the module-level canonical implementation."""
        return compute_deployed_hashes(rel_paths, self.ctx.project_root)
