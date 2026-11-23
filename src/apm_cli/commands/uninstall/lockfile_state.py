"""In-memory uninstall reconciliation for one atomic lockfile write."""

from __future__ import annotations

from pathlib import Path


def reconcile_uninstall_deployment_state(
    lockfile,
    *,
    deploy_root: Path,
    all_deployed_files: set[str],
    surviving_deployed_files: dict[str, set[str]],
    fully_refreshed_dependency_keys: set[str] | None = None,
) -> bool:
    """Reconcile survivor ownership and hashes without persisting."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.core.deployment_state import (
        DeploymentIntent,
        DeploymentReconciler,
        MaterializationResult,
        MaterializationStatus,
        NativePayloadValidation,
    )
    from apm_cli.install.phases.lockfile import compute_deployed_hashes
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.utils.diagnostics import DiagnosticCollector

    replacement_changed = False
    if lockfile.replacements is not None:
        surviving_keys = set(lockfile.dependencies)
        for dependency in lockfile.get_package_dependencies():
            if dependency.replacement is not None:
                replacement_changed |= dependency.replacement.retain_parents(surviving_keys)

    previous_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    records_by_value = {}
    for record in previous_ledger.records.values():
        records_by_value.setdefault(record.locator.value, []).append(record)
    materializations: list[MaterializationResult] = []
    for dep_key, deployed_files in surviving_deployed_files.items():
        transferred = sorted(all_deployed_files.intersection(deployed_files))
        hashes = compute_deployed_hashes(transferred, deploy_root)
        for path in transferred:
            for prior in records_by_value.get(path, ()):
                materializations.append(
                    MaterializationResult(
                        locator=prior.locator,
                        owners=frozenset({dep_key}),
                        status=MaterializationStatus.UNCHANGED,
                        content_hash=hashes.get(path),
                        validation=NativePayloadValidation(
                            valid=True,
                            contract="uninstall-survivor",
                        ),
                    )
                )

    reconciled = DeploymentReconciler(
        deploy_root,
        KNOWN_TARGETS,
        diagnostics=DiagnosticCollector(),
    ).reconcile(
        previous_ledger,
        materializations,
        DeploymentIntent(
            active_targets=frozenset(),
            declared_targets=None,
            desired_owners=DeploymentLedgerCodec.valid_owner_keys(lockfile),
            authoritative_targets=False,
        ),
    )
    changed = reconciled.changed or replacement_changed
    if reconciled.changed:
        DeploymentLedgerCodec.apply_to_lockfile(reconciled.ledger, lockfile)
    for dep_key in sorted(fully_refreshed_dependency_keys or set()):
        if dep_key not in lockfile.dependencies:
            continue
        deployed_files = sorted(surviving_deployed_files.get(dep_key, set()))
        hashes = compute_deployed_hashes(deployed_files, deploy_root)
        DeploymentLedgerCodec.replace_legacy_owner(
            lockfile,
            dep_key,
            deployed_files,
            hashes,
        )
        changed = True
    return changed


def retain_failed_claims(
    lockfile,
    removed_rows: dict,
    failed_paths,
    removed_hashes: dict[str, str],
    project_root: Path | None = None,
) -> bool:
    """Keep each removed row's claim on the files sync could not remove (#4851).

    Runs after MCP, LSP and native survivor derivation, so a claim-only row
    never keeps a service alive. The row returns narrowed to those files, with
    *removed_hashes* (the pre-removal cleanup snapshot, canonical ledger first)
    as the integrity baseline, through the ledger owner, so the lock keeps
    tracking what is still on disk and the next run can retry. Returns whether
    the lock changed.
    """
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    failed = {str(path).replace("\\", "/") for path in failed_paths}
    claims: dict[str, list[str]] = {}
    for key, row in removed_rows.items():
        kept = [path for path in row.deployed_files if path.replace("\\", "/") in failed]
        if kept and key not in lockfile.dependencies:
            claims[key] = kept
    present = set(lockfile.dependencies) | set(claims)
    # Each claim's local source is read while every removed parent is still visible.
    sources = _claim_local_sources(lockfile, removed_rows, claims, present, project_root)
    renamed = {}
    for key in claims:
        _anchor_claim_row(removed_rows[key], present, sources.get(key))
        # A legacy local row keyed by its local_path takes its re-spelled key.
        renamed[key] = removed_rows[key].get_unique_key()
    final = set(lockfile.dependencies) | set(renamed.values())
    for key in claims:
        row = removed_rows[key]
        row.resolved_by = renamed.get(row.resolved_by, row.resolved_by)
        row.declaring_parent = renamed.get(row.declaring_parent, row.declaring_parent)
        _anchor_claim_row(row, final)
    updates: dict[str, tuple[list[str], dict[str, str]]] = {}
    for key, kept in claims.items():
        row, owner = removed_rows[key], renamed[key]
        hashes = {}
        for path in kept:
            content_hash = removed_hashes.get(path) or row.deployed_file_hashes.get(path)
            if content_hash:
                hashes[path] = content_hash
        if owner in updates or owner in lockfile.dependencies:
            # Same identity as a row the lock keeps: its files join that row.
            files, known = updates.get(owner) or (
                lockfile.dependencies[owner].deployed_files,
                lockfile.dependencies[owner].deployed_file_hashes,
            )
            updates[owner] = ([*dict.fromkeys([*files, *kept])], {**hashes, **known})
            continue
        row.native_plugin = None
        lockfile.dependencies[owner] = row
        updates[owner] = (kept, hashes)
    DeploymentLedgerCodec.replace_legacy_owners(lockfile, updates)
    return bool(updates)


def _claim_local_sources(lockfile, removed_rows, claims, present, project_root) -> dict[str, str]:
    """Root-relative ``local_path`` of each claim whose declaring parent goes."""
    import copy

    from apm_cli.deps.path_anchoring import root_anchored_local_path

    if project_root is None:
        return {}
    view = copy.copy(lockfile)
    view.dependencies = {**removed_rows, **lockfile.dependencies}
    return {
        key: root_anchored_local_path(removed_rows[key], view, project_root)
        for key in claims
        if removed_rows[key].resolved_by not in (None, *present)
        and removed_rows[key].replacement is None
        and removed_rows[key].is_local_source
    }


def _anchor_claim_row(row, present: set[str], root_local_path: str | None = None) -> None:
    """Point a restored claim row's graph edges only at rows the lock keeps.

    A parent removed in the same uninstall is gone; the claim anchors at the
    root instead, so the replacement graph stays valid and the lock persists.
    A local claim takes *root_local_path*, its source re-spelled against the
    project root, so it still names the same directory.
    """
    if row.resolved_by is not None and row.resolved_by not in present:
        row.resolved_by = None
        if root_local_path is not None:
            row.local_path = root_local_path
    if row.declaring_parent is not None and row.declaring_parent not in present:
        row.declaring_parent = None
    binding = row.replacement
    if binding is not None:
        binding.retain_parents_or_root(present)


def lockfile_has_persisted_state(lockfile) -> bool:
    """Return whether uninstall should keep rather than remove the lockfile."""
    return bool(
        lockfile.dependencies
        or lockfile.mcp_servers
        or lockfile.lsp_servers
        or lockfile.local_deployed_files
        or lockfile.local_staged_files
        or lockfile.deployment_ledger.records
        or lockfile.replacements is not None
    )
