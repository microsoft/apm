"""Target-scoped manifest reconciliation shared by lockfile build sites.

On-disk stale cleanup is target-scoped: it preserves files belonging to
OTHER deploy targets (``phases/cleanup.py``). The lockfile manifest must
reconcile with the same symmetry. An ``apm install`` only governs its own
targets' deploy roots and URI schemes, so manifest entries written by a
prior install for OTHER targets must be PRESERVED rather than clobbered.

Without this symmetry a multi-target deploy (e.g. the ``copilot`` target
writing ``.github/`` + ``.agents/skills/`` files, then a later
``copilot-app`` install writing DB-URI rows) leaves the committed lockfile
single-target: the surviving on-disk files become orphaned from the
manifest and escape every manifest-driven audit gate -- deployed-files-
present, content-integrity, and drift (issue #1716).

Two manifest blocks need this reconciliation:

* per-dependency ``deployed_files`` / ``deployed_file_hashes``
  (``phases/lockfile.py``), and
* project-root ``local_deployed_files`` / ``local_deployed_file_hashes``
  (``phases/post_deps_local.py``).

Both import :func:`union_preserving` so the behaviour stays identical.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.deployment_state import DeploymentLedger
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.cleanup import CleanupResult
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector


def _surface_target_cleanup(
    logger: InstallLogger | None, dep_key: str, cleanup: CleanupResult
) -> None:
    """Report a target-contraction cleanup pass at default verbosity.

    Deleting a dropped target's deployed file is a destructive operation in
    the user's tracked workspace, so -- like every other caller of the
    cleanup chokepoint (see ``install/phases/cleanup.py``) -- it must be
    visible without ``--verbose``. Surfaces each user-edit skip as a yellow
    inline and the deletion count as an info line.
    """
    if logger is None:
        return
    for skipped in cleanup.skipped_user_edit:
        logger.cleanup_skipped_user_edit(skipped, dep_key)
    logger.stale_cleanup(dep_key, len(cleanup.deleted))


def install_governance(targets: list[TargetProfile]) -> tuple[set[str], set[str]]:
    """Return ``(file_prefixes, uri_schemes)`` governed by *targets*.

    Dedicated target roots govern their full subtree. The shared ``.agents``
    root is partitioned by primitive subdirectory so one active target cannot
    claim a declared sibling's files (for example, Copilot's
    ``.agents/skills`` versus Antigravity's ``.agents/rules``).

    ``uri_schemes`` is the set of lockfile URI schemes used by dynamic /
    user-machine targets (``copilot-app`` -> ``copilot-app-db://``,
    ``copilot-cowork`` -> ``cowork://``).
    """
    file_prefixes: set[str] = set()
    uri_schemes: set[str] = set()
    from apm_cli.integration.targets import target_lockfile_uri_schemes

    for target in targets or []:
        target_schemes = target_lockfile_uri_schemes(target)
        if target_schemes:
            uri_schemes.update(target_schemes)
            continue
        root = getattr(target, "root_dir", None)
        if root and str(root).rstrip("/") != ".agents":
            file_prefixes.add(str(root).rstrip("/") + "/")
        primitives = getattr(target, "primitives", None)
        if isinstance(primitives, dict):
            for mapping in primitives.values():
                deploy_root = getattr(mapping, "deploy_root", None)
                base = str(deploy_root or root or "").rstrip("/")
                if base != ".agents":
                    continue
                subdir = getattr(mapping, "subdir", None)
                if subdir:
                    file_prefixes.add(f"{base}/{str(subdir).strip('/')}/")
                    continue
                extension = getattr(mapping, "extension", None)
                if extension:
                    file_prefixes.add(f"{base}/{str(extension).strip('/')}")
                else:
                    # Compatibility for minimal TargetProfile stand-ins.
                    file_prefixes.add(f"{base}/")
        if str(root or "").rstrip("/") == ".agents":
            for generated in getattr(target, "generated_files", ()) or ():
                file_prefixes.add(f".agents/{str(generated).lstrip('/')}")
    return file_prefixes, uri_schemes


def is_governed_by_install(path: str, file_prefixes: set[str], uri_schemes: set[str]) -> bool:
    """Return ``True`` if *path* is owned by the current install's targets.

    File paths are matched by top-level directory; scheme URIs (e.g.
    ``copilot-app-db://``, ``cowork://``) are matched by their scheme.
    """
    if "://" in path:
        scheme = path.split("://", 1)[0] + "://"
        return scheme in uri_schemes
    return any(
        path.startswith(prefix) if prefix.endswith("/") else path == prefix
        for prefix in file_prefixes
    )


def union_preserving(
    current_files: list[str],
    current_hashes: dict[str, str],
    prior_files: list[str],
    prior_hashes: dict[str, str],
    targets: list[TargetProfile],
    declared_targets: list[TargetProfile] | None = None,
    on_ghost_drop: Callable[[str], None] | None = None,
    prior_ledger: DeploymentLedger | None = None,
    cleanup_retained_hashes: dict[str, str | None] | None = None,
    current_run_trusted: bool = True,
    owner: str = "legacy",
    include_ledger: bool = False,
) -> tuple[list[str], dict[str, str]] | tuple[list[str], dict[str, str], DeploymentLedger]:
    """Union the current install's manifest with preserved other-target entries.

    ``current_files`` / ``current_hashes`` describe what THIS install
    deployed (and thus governs). ``prior_files`` / ``prior_hashes`` come from
    the existing lockfile. Returns ``(files, hashes)`` -- the current entries
    plus any prior entries that belong to OTHER targets (not governed by this
    install). Entries the current install governs are authoritative, so a
    same-target reinstall still drops files removed from the package.

    ``declared_targets`` is the consumer's legitimate target universe --
    apm.yml-declared canonical targets plus the always-legitimate gated/dynamic
    targets -- independent of any ``--target`` narrowing (see
    ``phases.targets.declared_target_profiles``). When provided, a prior entry
    that belongs to NEITHER this install's targets NOR any of those targets is
    an inactive-target *ghost* (e.g. a dependency's
    package-declared ``windsurf`` paths the consumer never activates) and is
    DROPPED -- it can never be written on disk, so re-preserving it fails
    ``deployed-files-present`` forever on fresh checkouts (issue #2059). When
    An entry matching no registered target pattern is indeterminate and is
    preserved. When ``declared_targets`` is ``None`` (auto-detect or
    ``--target``-only consumers -- no declared universe to check against), the
    legacy preserve-all behaviour is kept so a genuine multi-target deploy is
    never clobbered (issue #1716).
    """
    from apm_cli.core.deployment_state import (
        DeploymentIntent,
        DeploymentLedger,
        DeploymentLocator,
        DeploymentReconciler,
        DeploymentRecord,
        LocatorKind,
        MaterializationResult,
        MaterializationStatus,
        NativePayloadValidation,
    )
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.utils.diagnostics import DiagnosticCollector

    active_by_name = {target.name: target for target in targets}
    declared_by_name = (
        {target.name: target for target in declared_targets}
        if declared_targets is not None
        else None
    )
    active_prefixes, active_schemes = install_governance(targets)
    cleanup_retained_hashes = cleanup_retained_hashes or {}

    def _target_for(path: str) -> str:
        ordered = [
            *targets,
            *(declared_targets or []),
            *KNOWN_TARGETS.values(),
        ]
        seen_names: set[str] = set()
        for profile in ordered:
            if profile.name in seen_names:
                continue
            seen_names.add(profile.name)
            prefixes, schemes = install_governance([profile])
            if is_governed_by_install(path, prefixes, schemes):
                return profile.name
        return "legacy"

    def _locator(path: str) -> DeploymentLocator:
        return DeploymentLocator(
            kind=LocatorKind.URI if "://" in path else LocatorKind.PROJECT_RELATIVE,
            target=_target_for(path),
            value=path,
            runtime=None,
            scope="project",
        )

    prior_values = set(prior_files or ())
    prior_records = {
        key: record
        for key, record in (prior_ledger.records.items() if prior_ledger is not None else ())
        if record.locator.value in prior_values
    }
    prior_record_values = {record.locator.value for record in prior_records.values()}
    for path in prior_files or ():
        if path in prior_record_values:
            continue
        locator = _locator(path)
        prior_records[locator.key] = DeploymentRecord(
            locator=locator,
            owners=(owner,),
            active_owner=owner,
            content_hash=prior_hashes.get(path),
        )
        prior_record_values.add(path)
    current_results = [
        MaterializationResult(
            locator=_locator(path),
            owners=frozenset({owner}),
            status=MaterializationStatus.UNCHANGED,
            content_hash=current_hashes.get(path),
            validation=NativePayloadValidation(valid=True, contract="legacy-file"),
        )
        for path in current_files or ()
        if path not in cleanup_retained_hashes
    ]
    current_results.extend(
        MaterializationResult(
            locator=_locator(path),
            owners=frozenset({owner}),
            status=MaterializationStatus.FAILED,
            content_hash=prior_hashes.get(path),
            validation=NativePayloadValidation(valid=True, contract="legacy-file"),
        )
        for path in cleanup_retained_hashes
        if path in current_files
    )
    reconciled = DeploymentReconciler(
        Path.cwd(),
        KNOWN_TARGETS,
        diagnostics=DiagnosticCollector(),
    ).reconcile(
        DeploymentLedger(records=prior_records),
        current_results,
        DeploymentIntent(
            active_targets=frozenset(active_by_name),
            declared_targets=(
                frozenset(declared_by_name) if declared_by_name is not None else None
            ),
            desired_owners=None,
            authoritative_targets=current_run_trusted,
            generic_governed_values=(
                frozenset(
                    path
                    for path in prior_values
                    if is_governed_by_install(path, active_prefixes, active_schemes)
                )
                if (
                    current_run_trusted
                    and declared_by_name is not None
                    and set(active_by_name) == set(declared_by_name)
                )
                else None
            ),
        ),
    )
    retained_values = {record.locator.value for record in reconciled.ledger.records.values()}
    current_set = set(current_files or ())
    if on_ghost_drop is not None:
        for locator in reconciled.removed:
            if (
                locator.value not in current_set
                and locator.target in KNOWN_TARGETS
                and locator.target not in active_by_name
            ):
                on_ghost_drop(locator.value)
    preserved = [
        path for path in prior_files or () if path not in current_set and path in retained_values
    ]
    merged_hashes = dict(current_hashes or {})
    for path in cleanup_retained_hashes:
        if path in current_set and path in prior_hashes:
            merged_hashes[path] = prior_hashes[path]
    for path in preserved:
        if path in prior_hashes:
            merged_hashes[path] = prior_hashes[path]
    files = list(current_files or ()) + preserved
    if include_ledger:
        return files, merged_hashes, reconciled.ledger
    return files, merged_hashes


def declared_target_profiles(
    project_root: Path,
    *,
    user_scope: bool = False,
) -> list[TargetProfile] | None:
    """Resolve the target universe declared by a project manifest."""
    from apm_cli.core.apm_yml import CANONICAL_TARGETS, parse_targets_field
    from apm_cli.core.errors import TargetResolutionError
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.utils.yaml_io import load_yaml

    try:
        data = load_yaml(project_root / "apm.yml")
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        names = parse_targets_field(data)
    except TargetResolutionError:
        return None
    if not names:
        return None

    profiles: list[TargetProfile] = []
    for name in dict.fromkeys(names):
        profile = KNOWN_TARGETS.get(name)
        if profile is None:
            continue
        scoped = profile.for_scope(user_scope=user_scope)
        if scoped is not None:
            profiles.append(scoped)
    for name, profile in KNOWN_TARGETS.items():
        if name in CANONICAL_TARGETS:
            continue
        scoped = profile.for_scope(user_scope=user_scope)
        profiles.append(scoped if scoped is not None else profile)
    return profiles or None


def reconcile_dropped_merge_hook_targets(
    project_root: Path,
    active_targets: list[TargetProfile],
    declared_targets: list[TargetProfile] | None,
    *,
    user_scope: bool = False,
) -> dict[str, int]:
    """Clean merge-hook JSON/sidecar state for targets dropped from ``targets:``.

    Complements :func:`reconcile_deployed_state` for the one class of
    APM-owned state that function structurally cannot see: merge-hook
    config files (``.codex/hooks.json``, ``.claude/settings.json``, etc.)
    are deliberately excluded from ``deployed_files`` tracking -- ownership
    lives in an ``_apm_source`` marker / ``apm-hooks.json`` ownership
    sidecar instead of a tracked path list (see
    ``apm_cli.integration.hook_integrator.HookIntegrator``). Narrowing a
    project's declared ``targets:`` (e.g. ``[claude, codex]`` ->
    ``[claude]``) therefore never reached this state before: prune/uninstall
    intentionally scope their merge-hook wipe to the SAME resolved target
    set as rebuild (#2250/#2252) and never clean a dropped target's residue
    by design.

    Mirrors :func:`reconcile_deployed_state`'s own
    ``allowed = active_targets union declared_targets`` semantics exactly,
    including the ``declared_targets is None`` legacy preserve-all no-op
    (issue #2059 symmetry: with no declared universe there is no dropped-
    target detection to perform). A known target name outside that allowed
    set is a candidate drop; this function only computes WHICH names
    dropped -- exactly as :func:`reconcile_deployed_state` already does for
    file-based state -- and delegates ALL merge-hook JSON/sidecar mutation
    to ``HookIntegrator.reconcile_dropped_targets``, the sole owner of that
    state. Names that are not merge-hook targets at all (e.g. ``copilot``,
    already correctly cleaned via the generic ``deployed_files``
    reconciliation) are silently skipped by that owner.
    """
    if declared_targets is None:
        return {"files_removed": 0, "errors": 0}

    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.targets import KNOWN_TARGETS

    allowed_names = {t.name for t in active_targets} | {t.name for t in declared_targets}
    dropped_names = [name for name in KNOWN_TARGETS if name not in allowed_names]
    if not dropped_names:
        return {"files_removed": 0, "errors": 0}
    return HookIntegrator().reconcile_dropped_targets(
        project_root, dropped_names, user_scope=user_scope
    )


def reconcile_deployed_block(  # noqa: PLR0913 -- deployed-state chokepoint wrapper; each knob is orthogonal
    *,
    project_root: Path,
    dep_key: str,
    current_files: list[str],
    current_hashes: dict[str, str],
    prior_files: list[str],
    prior_hashes: dict[str, str],
    active_targets: list[TargetProfile],
    declared_targets: list[TargetProfile] | None,
    diagnostics: DiagnosticCollector,
    on_ghost_drop: Callable[[str], None] | None = None,
    on_cleanup: Callable[[CleanupResult], None] | None = None,
    prior_ledger: DeploymentLedger | None = None,
    cleanup_retained_hashes: dict[str, str | None] | None = None,
    current_run_trusted: bool = True,
    owner: str = "legacy",
    include_ledger: bool = False,
    apply_disk_deletion: bool = True,
) -> tuple[list[str], dict[str, str]] | tuple[list[str], dict[str, str], DeploymentLedger]:
    """Reconcile one deployed-state block and safely remove dropped paths.

    When *apply_disk_deletion* is ``False`` (``apm lock`` / ``lockfile_only``)
    the block is reconciled without touching the working tree: every path that
    would otherwise be pruned is retained in the returned rows/hashes/ledger so
    the lockfile keeps mirroring on-disk reality and the drop signal survives to
    the next ``apm install``, which performs the physical prune through the
    ``remove_stale_deployed_files`` chokepoint (issue #2296).
    """
    files, hashes, ledger = union_preserving(
        current_files,
        current_hashes,
        prior_files,
        prior_hashes,
        active_targets,
        declared_targets=declared_targets,
        on_ghost_drop=on_ghost_drop,
        prior_ledger=prior_ledger,
        cleanup_retained_hashes=cleanup_retained_hashes,
        current_run_trusted=current_run_trusted,
        owner=owner,
        include_ledger=True,
    )
    dropped = set(prior_files) - set(files)
    # Ledger-authoritative orphan detection. A prior value that HELD a ledger
    # row, LOST it during reconciliation (its owning target went stale), yet
    # still lingers in `files` -- a stale-cleanup retention re-inserts shared
    # roots the active target cannot validate -- is a physical orphan. It never
    # entered `dropped` (still present in `files`) so it would otherwise survive
    # on disk with no manifest row. Route it through the SAME cleanup chokepoint
    # so the validate / provenance / unlink gates decide its fate. This acts
    # only on values that had a prior row (never on a ghost/no-row path), and
    # excludes any value re-owned this run (`surviving`), so a shared root a
    # concrete active target still claims is preserved.
    if prior_ledger is not None:
        prior_owned = {record.locator.value for record in prior_ledger.records.values()}
        surviving = {record.locator.value for record in ledger.records.values()}
        dropped |= (set(prior_files) & prior_owned) - surviving
    if not dropped:
        if include_ledger:
            return files, hashes, ledger
        return files, hashes

    if not apply_disk_deletion:
        # Lock mode (`apm lock`): reconcile lockfile rows but never unlink.
        # Retain every dropped path (row, hash, and prior ledger record) so the
        # lockfile keeps mirroring what remains on disk and the drop signal is
        # preserved for the next `apm install`, which does the physical prune.
        for path in dropped:
            if path not in files:
                files.append(path)
            if path in prior_hashes:
                hashes[path] = prior_hashes[path]
        if prior_ledger is not None:
            from apm_cli.core.deployment_state import DeploymentLedger

            dropped_values = set(dropped)
            records = dict(ledger.records)
            records.update(
                {
                    key: record
                    for key, record in prior_ledger.records.items()
                    if record.locator.value in dropped_values
                }
            )
            ledger = DeploymentLedger(records=records)
        if include_ledger:
            return files, hashes, ledger
        return files, hashes

    from apm_cli.integration.base_integrator import BaseIntegrator
    from apm_cli.integration.cleanup import remove_stale_deployed_files

    cleanup = remove_stale_deployed_files(
        dropped,
        project_root,
        dep_key=dep_key,
        targets=None,
        diagnostics=diagnostics,
        recorded_hashes=prior_hashes,
    )
    if on_cleanup is not None:
        on_cleanup(cleanup)
    # Orphan candidates (unlike the classic dropped set) can still be present in
    # `files`; a proven deletion must therefore also retract the value from the
    # returned manifest and hashes so the lockfile row and disk state agree.
    if cleanup.deleted:
        deleted = set(cleanup.deleted)
        files = [path for path in files if path not in deleted]
        for path in deleted:
            hashes.pop(path, None)
    for path in cleanup.retained:
        if path not in files:
            files.append(path)
        if path in prior_hashes:
            hashes[path] = prior_hashes[path]
    if cleanup.retained and prior_ledger is not None:
        from apm_cli.core.deployment_state import DeploymentLedger

        retained_values = set(cleanup.retained)
        records = dict(ledger.records)
        records.update(
            {
                key: record
                for key, record in prior_ledger.records.items()
                if record.locator.value in retained_values
            }
        )
        ledger = DeploymentLedger(records=records)
    if cleanup.deleted_targets:
        BaseIntegrator.cleanup_empty_parents(cleanup.deleted_targets, project_root)
    if include_ledger:
        return files, hashes, ledger
    return files, hashes


def reconcile_target_deployed_files(
    *,
    project_root: Path,
    lockfile: LockFile,
    active_targets: list[TargetProfile],
    declared_targets: list[TargetProfile] | None,
    diagnostics: DiagnosticCollector,
    user_scope: bool = False,
    logger: InstallLogger | None = None,
) -> bool:
    """Prune undeclared-target file ownership from every lockfile deployment block.

    This is the canonical target-contraction owner for physical deployed
    files. It calculates stale target-owned rows and delegates every deletion
    to :func:`reconcile_deployed_block`, which routes through the cleanup
    chokepoint and its path, directory, and provenance safety gates. When a
    *logger* is supplied, each deletion pass is surfaced at default verbosity
    (deletion count + user-edit skips) so the destructive workspace change is
    never silent.
    """
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.deps.lockfile import _SELF_KEY
    from apm_cli.integration.targets import KNOWN_TARGETS

    allowed_targets = [*active_targets, *(declared_targets or [])]
    allowed_prefixes, allowed_schemes = install_governance(allowed_targets)
    known_prefixes, known_schemes = install_governance(list(KNOWN_TARGETS.values()))

    def _retained(files: list[str]) -> list[str]:
        if declared_targets is None:
            return list(files)
        return [
            path
            for path in files
            if not (
                is_governed_by_install(path, known_prefixes, known_schemes)
                and not is_governed_by_install(path, allowed_prefixes, allowed_schemes)
            )
        ]

    changed = False
    prior_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    for dep_key, dependency in lockfile.dependencies.items():
        if dep_key == _SELF_KEY:
            continue
        prior_files = list(dependency.deployed_files)
        prior_hashes = dict(dependency.deployed_file_hashes)
        current_files = _retained(prior_files)
        current_hashes = {
            path: value for path, value in prior_hashes.items() if path in current_files
        }
        files, hashes = reconcile_deployed_block(
            project_root=project_root,
            dep_key=dep_key,
            current_files=current_files,
            current_hashes=current_hashes,
            prior_files=prior_files,
            prior_hashes=prior_hashes,
            active_targets=active_targets,
            declared_targets=declared_targets,
            diagnostics=diagnostics,
            prior_ledger=prior_ledger,
            on_cleanup=partial(_surface_target_cleanup, logger, dep_key),
        )
        if files != prior_files or hashes != prior_hashes:
            DeploymentLedgerCodec.replace_legacy_owner(lockfile, dep_key, files, hashes)
            changed = True

    prior_local = list(lockfile.local_deployed_files)
    prior_local_hashes = dict(lockfile.local_deployed_file_hashes)
    current_local = _retained(prior_local)
    current_local_hashes = {
        path: value for path, value in prior_local_hashes.items() if path in current_local
    }
    local_files, local_hashes = reconcile_deployed_block(
        project_root=project_root,
        dep_key="<local .apm/>",
        current_files=current_local,
        current_hashes=current_local_hashes,
        prior_files=prior_local,
        prior_hashes=prior_local_hashes,
        active_targets=active_targets,
        declared_targets=declared_targets,
        diagnostics=diagnostics,
        prior_ledger=prior_ledger,
        on_cleanup=partial(_surface_target_cleanup, logger, "<local .apm/>"),
    )
    if local_files != prior_local or local_hashes != prior_local_hashes:
        DeploymentLedgerCodec.replace_legacy_owner(lockfile, ".", local_files, local_hashes)
        changed = True

    return changed


def reconcile_deployed_state(
    *,
    project_root: Path,
    lockfile: LockFile,
    active_targets: list[TargetProfile],
    declared_targets: list[TargetProfile] | None,
    diagnostics: DiagnosticCollector,
    user_scope: bool = False,
) -> bool:
    """Reconcile target-scoped deployed files and merge-hook state."""
    changed = reconcile_target_deployed_files(
        project_root=project_root,
        lockfile=lockfile,
        active_targets=active_targets,
        declared_targets=declared_targets,
        diagnostics=diagnostics,
        user_scope=user_scope,
    )
    reconcile_dropped_merge_hook_targets(
        project_root,
        active_targets=active_targets,
        declared_targets=declared_targets,
        user_scope=user_scope,
    )
    return changed


def reconcile_project_deployed_state(
    manifest_root: Path,
    *,
    explicit_target: str | list[str] | None,
    deploy_root: Path | None = None,
    lock_root: Path | None = None,
    user_scope: bool = False,
    verbose: bool = False,
) -> bool:
    """Reconcile and persist a project's deployed state after a command."""
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    from apm_cli.integration.targets import active_targets, active_targets_user_scope
    from apm_cli.utils.diagnostics import DiagnosticCollector

    deploy_root = deploy_root or manifest_root
    lock_path = get_lockfile_path(lock_root or manifest_root)
    lockfile = LockFile.read(lock_path)
    if lockfile is None:
        return False
    declared = declared_target_profiles(manifest_root, user_scope=user_scope)
    if explicit_target is None and declared is not None:
        targets = declared
    elif user_scope:
        targets = active_targets_user_scope(explicit_target=explicit_target)
    else:
        targets = active_targets(deploy_root, explicit_target=explicit_target)
    changed = reconcile_deployed_state(
        project_root=deploy_root,
        lockfile=lockfile,
        active_targets=targets,
        declared_targets=declared,
        diagnostics=DiagnosticCollector(verbose=verbose),
        user_scope=user_scope,
    )
    if changed:
        lockfile.save(lock_path)
    return changed
