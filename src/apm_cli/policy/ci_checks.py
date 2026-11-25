"""Baseline CI checks for lockfile consistency.

These checks run without any policy file -- they validate that the on-disk
state matches what the lockfile declares.  This is the "Terraform plan for
agent config" gate: if anything is out of sync, the check fails and the CI
pipeline should block the merge.

Exit-code contract (consumed by the ``apm audit --ci`` command):
  * All checks pass -> exit 0
  * Any check fails  -> exit 1
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Sequence, cast  # noqa: UP035

from apm_cli.core.scope import manifest_file

from ..core import remediation
from ..core.deployment_ledger import DEPLOYMENT_OWNER_REMEDIATION
from ..core.remediation import DEPLOYED_FILES_MISSING, LOCKFILE_MISSING, ORPHANED_PACKAGES
from ..deps.lockfile import _SELF_KEY, LEGACY_LOCKFILE_NAME, LOCKFILE_NAME
from .models import CheckResult, CIAuditResult

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Any

    from ..deps.lockfile import LockFile
    from ..install.audit_replay import PreparedCiAuditReplay
    from ..install.drift import DriftFinding
    from ..integration.mcp_config_view import CurrentMcpConfigView
    from ..integration.targets import TargetProfile
    from ..models.apm_package import APMPackage
    from ..models.dependency.reference import DependencyReference
    from ..security.trust import McpAdmission

_logger = logging.getLogger(__name__)


# -- Individual checks ---------------------------------------------


def _read_audited_lock(lockfile_path: Path) -> tuple[Any, CheckResult | None]:
    """Read the audited lockfile; a version this APM cannot read is a failed check."""
    from ..deps.lockfile import LockFile, UnsupportedLockfileVersionError

    try:
        return LockFile.read(lockfile_path), None
    except UnsupportedLockfileVersionError as exc:
        # A lockfile from a newer APM fails the gate and names both remedies (#4233).
        return None, CheckResult(
            name="lockfile-exists",
            passed=False,
            message=str(exc),
            remediations=getattr(exc, "remediations", ()),
        )


def _check_lockfile_exists(
    project_root: Path,
    manifest: APMPackage | None,
) -> CheckResult:
    """Check that ``apm.lock.yaml`` is present when relevant.

    Receives the already-parsed manifest from :func:`run_baseline_checks`
    (``None`` when no ``apm.yml`` exists on disk).  This function never
    parses ``apm.yml`` itself and always returns ``name="lockfile-exists"``.

    Relevance is determined by either:
      * the manifest declaring APM/MCP dependencies, or
      * a lockfile already on disk recording local-only content
        (``local_deployed_files``) for this project.
    """
    from ..deps.lockfile import LockFile, get_lockfile_path

    if manifest is None:
        return CheckResult(
            name="lockfile-exists",
            passed=True,
            message="No apm.yml found -- nothing to check",
        )

    from ..install.plan import manifest_requires_lockfile

    has_deps = manifest_requires_lockfile(manifest)
    lockfile_path = get_lockfile_path(project_root)

    # Local-only repos may declare no remote/MCP deps but still have a
    # lockfile recording the project's own local content (synthesized as
    # the "." self-entry).  Treat that as having deps so downstream audit
    # checks (deployed-files-present, content-integrity) still run.
    if not has_deps and lockfile_path.exists():
        try:
            lock_for_gating = LockFile.read(lockfile_path)
            if lock_for_gating is not None and lock_for_gating.local_deployed_files:
                has_deps = True
        except Exception as exc:
            _logger.debug("Could not read lockfile for gating: %s", exc)

    if not has_deps:
        return CheckResult(
            name="lockfile-exists",
            passed=True,
            message="No dependencies declared -- lockfile not required",
        )

    if lockfile_path.exists():
        return CheckResult(
            name="lockfile-exists",
            passed=True,
            message="Lockfile present",
        )

    return CheckResult(
        name="lockfile-exists",
        passed=False,
        message=f"Lockfile missing -- {LOCKFILE_MISSING.clause()}",
        details=["apm.yml declares dependencies but apm.lock.yaml is absent"],
        remediations=(LOCKFILE_MISSING,),
    )


def _check_ref_consistency(
    manifest: APMPackage,
    lock: LockFile,
) -> CheckResult:
    """Verify manifest refs match the lockfile's resolved ref and commit."""
    from ..drift import detect_ref_change, marketplace_ref_mismatch, revision_pin_mismatch
    from ..install.plan import lockfile_satisfies_replacements
    from ..models.dependency.replacement import project_manifest_dependencies

    _, mismatches = lockfile_satisfies_replacements(lock, manifest.replace_rules)
    requires_update = unlocked = False
    marketplace_index = lock.marketplace_index()
    for dep_ref in project_manifest_dependencies(manifest):
        key = dep_ref.get_unique_key()
        display = dep_ref.get_display_name() if dep_ref.is_marketplace else key
        locked_dep = lock.get_dependency(key, marketplace_index=marketplace_index)
        if locked_dep is None:
            if dep_ref.is_marketplace:
                pair = (dep_ref.marketplace_name, dep_ref.marketplace_plugin_name)
                reason = "ambiguous" if pair in marketplace_index else "missing"
                mismatches.append(
                    f"{display}: {reason} marketplace provenance in lockfile; "
                    "inspect the locked sources before reinstalling"
                )
                requires_update = requires_update or bool(lock.dependencies)
            else:
                mismatches.append(f"{key}: not found in lockfile")
                unlocked = True
            continue
        if dep_ref.is_marketplace:
            if reason := marketplace_ref_mismatch(dep_ref, locked_dep):
                mismatches.append(f"{display}: {reason}")
                requires_update = True
            continue
        if pin_reason := revision_pin_mismatch(dep_ref.reference, locked_dep):
            mismatches.append(f"{key}: {pin_reason}")
            requires_update = True
            continue
        if detect_ref_change(dep_ref, locked_dep):
            manifest_ref = dep_ref.reference or "(default branch)"
            locked_ref = locked_dep.resolved_ref or "(default branch)"
            mismatches.append(
                f"{key}: manifest ref '{manifest_ref}' != lockfile ref '{locked_ref}'"
            )

    if not mismatches:
        return CheckResult(
            name="ref-consistency",
            passed=True,
            message="All dependency refs match lockfile",
        )
    # A newly declared dependency is locked by a plain install. A direct lock
    # row the manifest no longer declares may be that dependency's identity
    # rewritten, so re-resolve everything from the manifest instead.
    if unlocked and not requires_update:
        requires_update = not _check_no_orphans(manifest, lock).passed
    repair_command = "apm update" if requires_update else "apm install"
    n = len(mismatches)
    noun = "mismatch" if n == 1 else "mismatches"
    return CheckResult(
        name="ref-consistency",
        passed=False,
        message=(f"{n} ref {noun} -- run '{repair_command}' to update lockfile"),
        details=mismatches,
    )


def _filter_gitignored(project_root: Path, rel_paths: list[str]) -> list[str]:
    """Return only paths from rel_paths that are NOT gitignored.

    A gitignored path is absent by design on a fresh clone and must not be
    reported as a missing deployment.  Uses ``git check-ignore --stdin -z``
    for reliable, gitignore-spec-compliant evaluation.

    Falls back to returning the original list unchanged when git is not
    available or when ``project_root`` is not inside a git repository, so
    the behaviour reverts to the pre-fix state rather than silently passing.

    Args:
        project_root: Absolute path to the root of the git repository.
        rel_paths: Paths relative to ``project_root`` to evaluate.

    Returns:
        The subset of ``rel_paths`` whose entries are NOT gitignored.
    """
    if not rel_paths:
        return rel_paths
    try:
        import subprocess

        from ..utils.git_env import get_git_executable, git_subprocess_env

        git_exe = get_git_executable()
        result = subprocess.run(
            [git_exe, "check-ignore", "--stdin", "-z"],
            input="\x00".join(rel_paths),
            cwd=str(project_root),
            env=git_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        # git check-ignore exits 0 if at least one path is ignored, 1 if none
        # are ignored, and 128 on fatal errors.  We only care about stdout.
        ignored = {p for p in result.stdout.split("\x00") if p}
        return [p for p in rel_paths if p not in ignored]
    except (OSError, subprocess.SubprocessError, ImportError):
        _logger.warning(
            "Could not query gitignore status for %d path(s); reporting all absent paths as missing",
            len(rel_paths),
        )
        return rel_paths


def _resolve_deployed_file_path(
    value: str,
    project_root: Path,
    targets: Sequence[TargetProfile],
) -> Path | None:
    """Resolve a lock path only within the workspace or a live target root."""
    from ..integration.base_integrator import BaseIntegrator
    from ..utils.path_security import PathTraversalError, ensure_path_within

    if "://" in value:
        return None
    candidate = Path(value.rstrip("/"))
    if not candidate.is_absolute():
        rel_path = candidate.as_posix()
        # Historical claims remain verifiable after current replay intent changes.
        allowed_prefixes = (
            *BaseIntegrator._get_integration_prefixes(),
            *BaseIntegrator._get_integration_prefixes(targets=targets),
        )
        if not BaseIntegrator.validate_deploy_path(
            rel_path, project_root, allowed_prefixes=allowed_prefixes
        ):
            return None
        return project_root / candidate
    for target in targets:
        deploy_root = getattr(target, "managed_deploy_root", None)
        if deploy_root is None:
            continue
        try:
            return ensure_path_within(candidate, deploy_root)
        except PathTraversalError:
            continue
    return None


def find_missing_deployed_files(
    project_root: Path,
    lock: LockFile,
    targets: Sequence[TargetProfile] = (),
) -> tuple[list[str], bool]:
    """Return the recorded deployed files absent from disk, and whether any were gitignored.

    Single owner shared by ``apm audit`` deployed-files-present and the
    ``apm install --frozen`` post-deploy check, so both give one answer.
    URI claims are skipped. Paths that are gitignored are absent by design
    (the repo owner chose not to commit them) and are not reported, which
    avoids false positives on fresh checkouts that gitignore a deploy directory.
    """
    missing: list[str] = []
    for _dep_key, dep in lock.dependencies.items():
        for rel_path in dep.deployed_files:
            if "://" in rel_path:
                continue
            abs_path = _resolve_deployed_file_path(rel_path, project_root, targets)
            if (
                abs_path is None
                or not abs_path.exists()
                or (Path(rel_path).is_absolute() and abs_path.is_symlink())
            ):
                missing.append(rel_path)
    if not missing:
        return missing, False
    filtered = _filter_gitignored(project_root, missing)
    return filtered, len(filtered) < len(missing)


def _check_deployed_files_present(
    project_root: Path,
    lock: LockFile,
    targets: Sequence[TargetProfile] = (),
) -> CheckResult:
    """Verify all files listed in lockfile deployed_files exist on disk."""
    missing, gitignored_skipped = find_missing_deployed_files(project_root, lock, targets)

    if not missing:
        msg = (
            "All deployed files present on disk (gitignored paths skipped)"
            if gitignored_skipped
            else "All deployed files present on disk"
        )
        return CheckResult(
            name="deployed-files-present",
            passed=True,
            message=msg,
        )
    n = len(missing)
    noun = "file" if n == 1 else "files"
    return CheckResult(
        name="deployed-files-present",
        passed=False,
        message=f"{n} deployed {noun} missing -- {DEPLOYED_FILES_MISSING.clause()}",
        details=missing,
        remediations=(DEPLOYED_FILES_MISSING,),
    )


def _check_exec_status_current(
    project_root: Path,
    lock: LockFile,
    manifest: APMPackage,
    targets: Sequence[TargetProfile],
    *,
    user_scope: bool,
    modules_root: Path,
    policy_provider: Callable[[], Any] | None,
) -> CheckResult:
    """Fail when a locked ``exec_status`` is no longer the gate's decision (#4053).

    Routes through the staleness owner ``apm update`` and ``apm install
    --frozen`` use; audit never re-derives trust here. The gate's decision
    depends on org policy, so without a *policy_provider* (``--no-policy``)
    the check is not evaluated. Indeterminate answers (the provider raising
    ``ExecPolicyUnavailable``, an unreadable package) are not failures.
    """
    from ..core.scope import scope_manifest_path
    from ..install.exec_gate import stale_exec_decisions

    if policy_provider is None:
        return CheckResult(
            name="exec-status-current",
            passed=True,
            message="Executables decisions not verified: org policy not consulted (--no-policy)",
        )
    stale, indeterminate = stale_exec_decisions(
        lock,
        modules_root,
        scope_manifest_path(project_root, user_scope=user_scope),
        policy_provider=policy_provider,
        targets=list(targets),
        fallback_allow_executables=getattr(manifest, "allow_executables", None),
    )
    if not indeterminate:
        from ..install.lsp.integration import withdrawn_bundle_lsp_for_scope

        stale = [
            *stale,
            *withdrawn_bundle_lsp_for_scope(
                lock, modules_root.parent, project_root, user_scope=user_scope, no_policy=False
            ),
        ]
    if not stale:
        return CheckResult(
            name="exec-status-current",
            passed=True,
            message=(
                "Executables decisions not verified: org policy unavailable or a package unreadable"
                if indeterminate
                else "Recorded executables decisions match the executables gate"
            ),
        )
    n = len(stale)
    noun = "package records" if n == 1 else "packages record"
    return CheckResult(
        name="exec-status-current",
        passed=False,
        message=(
            f"{n} {noun} an executables decision the gate no longer makes -- "
            f"{remediation.FROZEN_EXEC_DECISIONS_STALE.clause()}"
        ),
        details=[decision.describe() for decision in stale],
    )


def _check_deployment_ledger_owners(lock: LockFile) -> CheckResult:
    """Require every canonical deployment owner to resolve in the lockfile."""
    from ..core.deployment_ledger import DeploymentLedgerCodec

    violations = DeploymentLedgerCodec.owner_reference_violations(lock)
    if not violations:
        return CheckResult(
            name="deployment-ledger-owners",
            passed=True,
            message="All deployment ledger owners are valid",
        )

    details = []
    for violation in violations:
        invalid = ", ".join(violation.invalid_owners)
        active = (
            f"; invalid active owner {violation.invalid_active_owner}"
            if violation.invalid_active_owner is not None
            else ""
        )
        details.append(f"{violation.locator.key}: invalid owner(s) {invalid}{active}")
    return CheckResult(
        name="deployment-ledger-owners",
        passed=False,
        message=(
            f"{len(violations)} invalid deployment ownership record(s) -- "
            f"{DEPLOYMENT_OWNER_REMEDIATION}"
        ),
        details=details,
    )


def _check_lockfile_targets_current(
    project_root: Path, lock: LockFile, *, user_scope: bool
) -> CheckResult:
    """Verify apm.lock.yaml records no files of a target apm.yml dropped (#4517).

    The same dropped-target owner ``apm install --frozen`` refuses through, so
    audit and frozen agree and share one remedy.
    """
    from ..install.manifest_reconcile import dropped_target_files

    dropped = dropped_target_files(project_root, lock, user_scope=user_scope)
    if not dropped:
        return CheckResult(
            name="lockfile-targets-current",
            passed=True,
            message="Lockfile records only declared targets",
        )
    fix = remediation.FROZEN_TARGET_DROPPED
    targets = ", ".join(sorted(set(dropped.values())))
    return CheckResult(
        name="lockfile-targets-current",
        passed=False,
        message=(
            f"apm.lock.yaml records {len(dropped)} file(s) of target(s) {targets} "
            f"that apm.yml no longer declares -- {fix.clause()}"
        ),
        details=[f"{path} (target {dropped[path]})" for path in sorted(dropped)],
        remediations=(fix,),
    )


def _check_no_orphans(
    manifest: APMPackage,
    lock: LockFile,
) -> CheckResult:
    """Verify no packages in lockfile are absent from manifest.

    Only DIRECT dependencies (``depth == 1`` / no ``resolved_by``) are
    candidates for orphan detection. Transitive deps belong to a
    sub-package's manifest, not the root manifest, so the root manifest
    cannot make them go away by editing its dependency lists. This is the
    orphan owner's direct-entry projection of the install decision.
    """
    from ..install.orphans import OrphanOperation, decide_orphan_removal

    decision = decide_orphan_removal(
        operation=OrphanOperation.AUDIT,
        roots=(),
        candidate_universe=lock,
        installed_paths=None,
        resolved_keys=None,
        intended=manifest.get_all_apm_dependencies(),
        materialized=None,
        reachability=None,
        deployment_claims=None,
    )
    orphaned = [dep_key for dep_key in lock.dependencies if dep_key in decision.removed_keys]
    if not orphaned:
        return CheckResult(
            name="no-orphaned-packages",
            passed=True,
            message="No orphaned packages in lockfile",
        )
    return CheckResult(
        name="no-orphaned-packages",
        passed=False,
        message=(
            f"{len(orphaned)} orphaned package(s) in lockfile -- {ORPHANED_PACKAGES.clause()}"
        ),
        details=orphaned,
        remediations=(ORPHANED_PACKAGES,),
    )


def replay_failure_check(
    name: str, error: str, *, owner: str | None = None, label: str = "replay failed"
) -> CheckResult:
    """Report one failed install replay once: in full under *owner*, else a pointer.

    The replay error is a single finding; every check that needed the replay
    fails closed, but only the owning check carries its text (RA-005).
    """
    if owner is not None and owner != name:
        return CheckResult(
            name=name,
            passed=False,
            message=f"not verified: the install replay failed (reported under {owner})",
        )
    return CheckResult(name=name, passed=False, message=f"{label}; see details", details=[error])


def _check_skill_subset_consistency(
    manifest: APMPackage,
    lock: LockFile,
    project_root: Path,
    *,
    prepared_replay: PreparedCiAuditReplay | None = None,
    prepared_replay_error: str | None = None,
    replay_error_owner: str | None = None,
) -> CheckResult:
    """Verify locked selections match what the declared graph merges to.

    Every locked dependency's ``skill_subset``/``target_subset`` must equal
    the merge (canonical owner: ``merge_edge_selections``) of every edge the
    project's ``apm.yml`` and the materialized packages' manifests declare
    for it (#3735). A selection that cannot be recomputed is reported as
    unverified, never passed.
    """
    from ..constants import APM_MODULES_DIR
    from ..deps.reachability import declared_node_selections

    if prepared_replay_error is not None:
        return replay_failure_check(
            "skill-subset-consistency", prepared_replay_error, owner=replay_error_owner
        )
    modules_root = (
        prepared_replay.modules_root
        if prepared_replay is not None
        else project_root / APM_MODULES_DIR
    )
    declared = declared_node_selections(
        lock,
        project_root,
        modules_root,
        manifest.get_apm_dependencies(),
        dev_refs=manifest.get_dev_apm_dependencies(),
    )
    mismatches: list[str] = []
    for locked_dep in lock.get_package_dependencies():
        key = locked_dep.get_unique_key()
        lock_skills = sorted(locked_dep.skill_subset or ())
        lock_targets = sorted(locked_dep.target_subset or ())
        missing = _missing_recorded_skill_subset_paths(
            modules_root,
            locked_dep.to_dependency_ref(),
            locked_dep.package_type,
            lock_skills,
        )
        if missing:
            mismatches.append(
                f"{key}: recorded skill subset path(s) not found in package tree: "
                f"{', '.join(missing)}"
            )
            continue
        selection = declared.selections.get(key)
        expected_skills = list(selection.skill_subset or ()) if selection else []
        expected_targets = list(selection.target_subset or ()) if selection else []
        # An unrestricted declaration absorbs any declaration the walk could
        # not read, so only then is an incomplete walk still conclusive.
        absorbed = (
            selection is not None
            and selection.skill_subset is None
            and selection.target_subset is None
        )
        if selection is None or (not declared.complete and not absorbed):
            mismatches.append(
                f"{key}: selection unverified -- the manifests that declare it could "
                "not all be read"
            )
            continue
        if lock_skills != expected_skills:
            mismatches.append(
                f"{key}: declared skills {expected_skills} != lockfile skill_subset {lock_skills}"
            )
            continue
        if lock_targets != expected_targets:
            mismatches.append(
                f"{key}: declared targets {expected_targets} != lockfile target_subset "
                f"{lock_targets}"
            )
            continue
        try:
            locked_pairing = locked_dep.selection()
        except ValueError as exc:
            mismatches.append(f"{key}: invalid lockfile subset_pairings -- {exc}")
            continue
        if locked_pairing != selection.lock_selection():
            mismatches.append(
                f"{key}: declared skill-target pairing "
                f"{_pairing_summary(selection.lock_selection())} != lockfile "
                f"{_pairing_summary(locked_pairing)}"
            )

    if not mismatches:
        return CheckResult(
            name="skill-subset-consistency",
            passed=True,
            message="Skill subset selections match lockfile",
        )
    return CheckResult(
        name="skill-subset-consistency",
        passed=False,
        message=(
            f"{len(mismatches)} skill subset mismatch(es) -- regenerate lockfile (apm install)"
        ),
        details=mismatches,
    )


def _pairing_summary(pairing) -> str:
    """Render a selection as ``targets: skills`` groups ('all' for an omitted axis)."""
    return "; ".join(
        f"{', '.join(p.targets) if p.targets else 'all targets'}: "
        f"{', '.join(p.skills) if p.skills else 'all skills'}"
        for p in pairing.pairings
    )


def _missing_recorded_skill_subset_paths(
    modules_root: Path,
    dep_ref: DependencyReference,
    package_type: str | None,
    subset: list[str],
) -> tuple[str, ...]:
    """Return recorded skill subset paths absent from the materialized package."""
    if not subset:
        return ()

    from types import SimpleNamespace

    from ..install.outcome import missing_requested_components
    from ..integration.skill_integrator import SkillIntegrator
    from ..models.dependency.subsets import skill_subset_selects_all
    from ..models.validation import PackageType

    try:
        resolved_package_type = PackageType(package_type) if package_type else None
        package_info = SimpleNamespace(
            install_path=dep_ref.get_install_path(modules_root),
            package_type=resolved_package_type,
        )
        available = SkillIntegrator.available_skill_names(package_info)
    except (OSError, RuntimeError, ValueError):
        available = frozenset()
    if available is None:
        available = frozenset()
    if skill_subset_selects_all(subset):
        # ``skills: ["*"]`` needs only one skill to be present.
        return () if available else tuple(subset)
    return missing_requested_components(requested=subset, available=available)


def audit_mcp_view(
    manifest: APMPackage,
    lock: LockFile,
    *,
    modules_root: Path | None = None,
    no_policy: bool = False,
) -> tuple[CurrentMcpConfigView, McpAdmission]:
    """The MCP view audit judges: config consistency and MCP policy read one baseline.

    Consent is re-evaluated from apm.yml and org policy, never from the
    lockfile or personal config, so CI and laptops reach one verdict. Audit
    accepts structural omission evidence and re-checks consent.
    """
    from ..constants import APM_MODULES_DIR
    from ..integration.mcp_config_view import CurrentMcpConfigView
    from ..security.trust import McpAdmission

    project_root = manifest.package_path or Path.cwd()
    admission = McpAdmission.for_project(
        project_root, user_scope=False, include_user=False, no_policy=no_policy
    )
    view = CurrentMcpConfigView.select(
        manifest,
        lock,
        modules_root if modules_root is not None else project_root / APM_MODULES_DIR,
        admission=admission,
        recorded=lock,
        exact=False,
    )
    return view, admission


def _check_config_consistency(
    manifest: APMPackage,
    lock: LockFile,
    *,
    prepared_replay: PreparedCiAuditReplay | None = None,
    prepared_replay_error: str | None = None,
    replay_error_owner: str | None = None,
    no_policy: bool = False,
) -> CheckResult:
    """Verify MCP server configs match lockfile baseline."""
    if prepared_replay_error is not None:
        return replay_failure_check(
            "config-consistency",
            prepared_replay_error,
            owner=replay_error_owner or "skill-subset-consistency",
        )
    view, admission = audit_mcp_view(
        manifest,
        lock,
        modules_root=prepared_replay.modules_root if prepared_replay is not None else None,
        no_policy=no_policy,
    )
    policy_note = (
        f" Org policy unavailable ({admission.policy_unavailable}); consent was "
        "evaluated without org deny."
        if admission.policy_unavailable
        else ""
    )
    stored_configs = lock.mcp_configs or {}
    diff = view.diff(stored_configs)

    # A server parked because its definition changed since approval stays
    # visible in a passing audit (for example after a bot bumps a ref).
    changed = sorted(
        name
        for name, evidence in (lock.mcp_omissions or {}).items()
        if isinstance(evidence, dict) and evidence.get("reason") == "definition-changed"
    )
    changed_notice = (
        CheckResult(
            name="config-consistency",
            passed=True,
            message=(
                f"MCP configs match lockfile baseline; {len(changed)} server(s) parked "
                "because the definition changed since approval: "
                f"{', '.join(changed)}. Review: apm approve --pending{policy_note}"
            ),
        )
        if changed
        else None
    )

    # No MCP deps at all -- nothing to check
    if not view.configs and not stored_configs and not view.problems:
        if changed_notice is not None:
            return changed_notice
        return CheckResult(
            name="config-consistency",
            passed=True,
            message=f"No MCP configs to check{policy_note}",
        )

    details = [f"{problem.package_key}: {problem.message}" for problem in view.problems]

    # Preserve the established diagnostics while sourcing every partition from
    # the canonical symmetric diff.
    for name in sorted(diff.changed):
        details.append(f"{name}: config differs from lockfile baseline")

    provenance = lock.mcp_config_provenance or {}
    for name in sorted(diff.lock_only):
        owner = provenance.get(name)
        suffix = f" (declared by {owner})" if owner else ""
        details.append(f"{name}: in lockfile but not in manifest{suffix}")

    for name in sorted(diff.source_only):
        details.append(f"{name}: in manifest but not in lockfile")

    if not details:
        if changed_notice is not None:
            return changed_notice
        return CheckResult(
            name="config-consistency",
            passed=True,
            message=f"MCP configs match lockfile baseline{policy_note}",
        )
    return CheckResult(
        name="config-consistency",
        passed=False,
        message=(
            f"{len(details)} MCP config inconsistenc(ies) -- "
            "repeat 'apm install' with the intended selection "
            f"('--only apm' for skills-only) to reconcile{policy_note}"
        ),
        details=details,
    )


def _check_native_mcp_ownership(
    project_root: Path,
    lock: LockFile,
    targets: Sequence[TargetProfile],
    *,
    user_scope: bool,
) -> CheckResult:
    """Fail where ``install --frozen`` refuses a differing same-name native MCP entry (#4477)."""
    from ..install.mcp.native_entry import differing_unrecorded_native_mcp_entries

    pairs = differing_unrecorded_native_mcp_entries(
        lock,
        [target.name for target in targets],
        project_root=project_root,
        user_scope=user_scope,
    )
    if not pairs:
        return CheckResult(
            name="native-mcp-ownership",
            passed=True,
            message="No unrecorded native MCP entries differ from their declarations",
        )
    return CheckResult(
        name="native-mcp-ownership",
        passed=False,
        message=(
            f"{len(pairs)} existing native MCP entr(ies) differ from their declarations "
            "and their ownership is not recorded in apm.lock.yaml -- "
            f"{remediation.NATIVE_MCP_OWNERSHIP_UNRECORDED.clause()}"
        ),
        details=[
            f"MCP server {name!a} target {runtime!a}: the existing entry differs from "
            "its declaration"
            for name, runtime in pairs
        ],
    )


def _native_mcp_plugin_roots(
    project_root: Path,
    manifest: APMPackage,
    lock: LockFile,
    *,
    prepared_replay: PreparedCiAuditReplay | None,
    no_policy: bool,
) -> Mapping[str, str]:
    """Each declared MCP server's package root, where its native entry points.

    A scratch replay reads declarations from its own tree; a root inside it is
    rebound to the live ``apm_modules/`` copy install rendered the entry at.
    """
    from ..constants import APM_MODULES_DIR

    modules_root = prepared_replay.modules_root if prepared_replay is not None else None
    view, _ = audit_mcp_view(manifest, lock, modules_root=modules_root, no_policy=no_policy)
    roots = dict(view.plugin_roots)
    if modules_root is None:
        return roots
    scratch = modules_root.resolve()
    live = (project_root / APM_MODULES_DIR).resolve()
    for name, root in roots.items():
        bound = Path(root)
        if bound.is_relative_to(scratch):
            roots[name] = str(live.joinpath(*bound.parts[len(scratch.parts) :]))
    return roots


def _edited_native_mcp_entries(
    project_root: Path,
    lock: LockFile,
    targets: Sequence[TargetProfile],
    *,
    user_scope: bool,
    plugin_roots: Callable[[], Mapping[str, str]] | None = None,
) -> tuple[CheckResult, ...]:
    """Report recorded native MCP entries edited since APM wrote them (#4757).

    Empty when none was edited, so audit output is unchanged for every other project.
    *plugin_roots* is read only when a recorded declaration uses ``${CLAUDE_PLUGIN_ROOT}``.
    """
    from ..deps.plugin_root_render import contains_plugin_root
    from ..install.mcp.native_entry import edited_recorded_native_mcp_entries

    roots = (
        plugin_roots()
        if plugin_roots is not None and contains_plugin_root(lock.mcp_configs or {})
        else None
    )
    edited = edited_recorded_native_mcp_entries(
        lock,
        [target.name for target in targets],
        project_root=project_root,
        user_scope=user_scope,
        plugin_roots=roots,
    )
    if not edited:
        return ()
    fix = remediation.NATIVE_MCP_ENTRY_EDITED
    if user_scope:
        fix = fix.with_command("apm install -g")
    check = CheckResult(
        name="native-mcp-drift",
        passed=False,
        message=(
            f"{len(edited)} native MCP entr(ies) recorded in apm.lock.yaml changed since "
            f"APM wrote them -- {fix.clause()}"
        ),
        remediations=(fix,),
        details=[
            f"MCP server {name!a} in {config} ({runtime}): the entry differs from what "
            "APM recorded writing"
            for name, runtime, config in edited
        ],
    )
    return (check,)


def _kept_mcp_entries(
    project_root: Path, lock: LockFile, *, user_scope: bool, lockless: bool = False
) -> tuple[CheckResult, ...]:
    """Report MCP entries install kept without a lockfile because it could not prove them (#4737).

    Empty when nothing was kept, so audit output is unchanged for every other project.
    """
    from ..install.mcp.write_receipts import kept_mcp_writes, unusable_mcp_evidence

    kept = kept_mcp_writes(
        project_root, user_scope=user_scope, managed=lock.mcp_servers, configs=lock.mcp_configs
    )
    details = [f"MCP server {write.name!a} in {config} ({write.runtime})" for write, config in kept]
    # Without a lockfile, unusable evidence keeps every entry it lists, so it is
    # reported; with one, only evidence an earlier lockless install met (and
    # kept every entry for) is still reported.
    unusable = unusable_mcp_evidence(project_root, user_scope=user_scope, noted_only=not lockless)
    if unusable is not None:
        details.append(unusable)
    if not details:
        return ()
    check = CheckResult(
        name="kept-mcp-entries",
        passed=False,
        message=(
            f"{len(details)} MCP finding(s): APM kept entries it wrote and no longer "
            "deploys -- "
            f"{remediation.KEPT_MCP_ENTRIES.clause()}"
        ),
        remediations=(remediation.KEPT_MCP_ENTRIES,),
        details=details,
    )
    return (check,)


@dataclass(frozen=True)
class DeployedHashDrift:
    """One deployed file whose on-disk bytes differ from its locked hash envelope."""

    owner: str
    path: str
    expected: str
    actual: str


def resolve_audit_scope(
    project_root: Path, lock: LockFile | None
) -> tuple[Path, bool, Sequence[TargetProfile]]:
    """Return the deployment root, user-scope flag and targets audit verifies against."""
    from ..core.scope import get_workspace_deploy_root
    from ..deps.lockfile import LockFile
    from ..integration.targets import resolve_audit_targets

    deployment_root = get_workspace_deploy_root(project_root)
    user_scope = deployment_root != project_root.resolve()
    targets = resolve_audit_targets(
        project_root,
        user_scope=user_scope,
        lockfile=lock if lock is not None else LockFile(),
    )
    return deployment_root, user_scope, targets


def resolve_claim_scope(
    project_root: Path, lock: LockFile
) -> tuple[Path, bool, list[TargetProfile]]:
    """:func:`resolve_audit_scope` with targets that resolve every recorded claim (#5312).

    The one call the frozen install checks share with audit.
    """
    deploy_root, user_scope, targets = resolve_audit_scope(project_root, lock)
    claim_targets = resolve_claim_targets(
        project_root, lock, user_scope=user_scope, targets=targets
    )
    return deploy_root, user_scope, claim_targets


def resolve_claim_targets(
    project_root: Path,
    lock: LockFile,
    *,
    user_scope: bool,
    targets: Sequence[TargetProfile],
) -> list[TargetProfile]:
    """Return the targets whose live roots resolve the lock's recorded claims (#5312).

    At user scope a claim may sit under the relocated root (``CLAUDE_CONFIG_DIR``)
    of a target the lock recorded but the manifest does not declare, from an
    ``install -g --target`` deploy. Those targets resolve through the same
    owner install uses; the audited target set itself is unchanged.
    """
    from ..core.deployment_ledger import DeploymentLedgerCodec
    from ..core.scope import get_workspace_deploy_root
    from ..integration.skill_layout import recorded_deployed_paths, resolve_reintegration_targets
    from ..integration.targets import KNOWN_TARGETS

    audited = {target.name for target in targets}
    recorded = {
        record.locator.target
        for record in DeploymentLedgerCodec.from_lockfile(lock).records.values()
    }
    undeclared = sorted(recorded.intersection(KNOWN_TARGETS).difference(audited))
    if not user_scope or not undeclared:
        return list(targets)
    return [
        *targets,
        *resolve_reintegration_targets(
            get_workspace_deploy_root(project_root),
            user_scope=True,
            explicit_target=undeclared,
            recorded_paths=recorded_deployed_paths(lock),
            create_config=False,
        ),
    ]


def verify_deployed_file_hashes(
    deploy_root: Path,
    lock: LockFile,
    targets: Sequence[TargetProfile] = (),
) -> tuple[list[DeployedHashDrift], list[str]]:
    """Re-verify every locked deployed-file hash against on-disk bytes (req-lk-017).

    Single owner shared by ``apm audit`` content-integrity and the
    ``apm install --frozen`` post-deploy check. Returns the hash
    mismatches and the claimed paths that resolve unsafely. Relative claims
    resolve against *deploy_root*, the live deployment root. Missing files
    are skipped: ``deployed-files-present`` owns that signal.
    """
    from ..core.deployment_ledger import DeploymentLedgerCodec
    from ..core.deployment_state import LocatorKind
    from ..integration.targets import KNOWN_TARGETS
    from ..utils.content_hash import compute_file_hash

    ledger = DeploymentLedgerCodec.from_lockfile(lock)

    def _has_symlink_component(path: Path) -> bool:
        try:
            relative = path.relative_to(deploy_root)
        except ValueError:
            return path.is_symlink()
        current = deploy_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return True
        return False

    # Per-file hash verification across canonical deployment records.
    hash_mismatches: list[DeployedHashDrift] = []
    unresolved_hash_paths: list[str] = []
    for record in ledger.records.values():
        expected_hash = record.content_hash
        if expected_hash is None:
            continue
        locator = record.locator
        if locator.kind == LocatorKind.URI:
            continue
        if locator.kind == LocatorKind.PROJECT_RELATIVE:
            file_path = _resolve_deployed_file_path(locator.value, deploy_root, targets)
            if file_path is None:
                unresolved_hash_paths.append(locator.value)
                continue
        else:
            target = next(
                (candidate for candidate in targets if candidate.name == locator.target),
                KNOWN_TARGETS.get(locator.target),
            )
            if target is None:
                continue
            try:
                resolved = DeploymentLedgerCodec.resolve_locator(
                    locator,
                    project_root=deploy_root,
                    target=target,
                )
            except (OSError, RuntimeError, ValueError):
                unresolved_hash_paths.append(locator.value)
                continue
            if isinstance(resolved, str):
                unresolved_hash_paths.append(locator.value)
                continue
            file_path = resolved
        if not file_path.exists():
            continue  # _check_deployed_files_present owns this signal
        # A locked file replaced by a symlink is never followed: it fails the
        # check (#3994). Only a symlinked parent directory (a shared root) skips.
        if (
            locator.kind == LocatorKind.PROJECT_RELATIVE
            and not file_path.is_symlink()
            and _has_symlink_component(file_path)
        ):
            continue
        if file_path.is_symlink() or not file_path.is_file():
            unresolved_hash_paths.append(locator.value)
            continue
        actual_hash = compute_file_hash(file_path)
        if actual_hash != expected_hash:
            hash_mismatches.append(
                DeployedHashDrift(
                    record.active_owner or "", locator.value, expected_hash, actual_hash
                )
            )

    return hash_mismatches, unresolved_hash_paths


def _local_content_drift(project_root: Path, lock: LockFile) -> list[str]:
    """Name local dependencies whose content differs from the lock, as frozen does."""
    from ..core.scope import manifest_file
    from ..install.local_content_verdict import local_content_reasons
    from ..models.apm_package import APMPackage
    from ..models.dependency.replacement import project_manifest_dependencies
    from ..models.manifest_io import InvalidManifestError

    path = manifest_file(project_root)
    if not path.is_file():
        return []
    try:
        manifest = APMPackage.from_apm_yml(path)
    except (InvalidManifestError, OSError, ValueError):
        return []  # The manifest checks report an unreadable apm.yml.
    reasons = local_content_reasons(manifest, lock, project_manifest_dependencies(manifest))
    return [reason.strip().removeprefix("- ") for reason in reasons]


def _check_content_integrity(
    project_root: Path,
    lock: LockFile,
    targets: Sequence[TargetProfile] = (),
    *,
    user_scope: bool = False,
) -> CheckResult:
    """Check deployed files for critical hidden Unicode and hash drift.

    Two signals are evaluated:
      * Critical hidden Unicode (steganographic markers) via the file
        scanner, over every file under the deploy trees the project's
        targets govern -- NOT only the files the lockfile records, since
        this signal needs no recorded baseline (see
        ``scan_deployed_trees``).
      * SHA-256 drift between the on-disk content and the canonical deployment
        ledger hash recorded at install time.
      * SHA-256 drift of local bundle compile staging (#3718), via
        ``LockFile.staged_hash_drift``.
      * Missing canonical ownership metadata for a legacy deployed-file hash.

    Missing files are deliberately skipped here -- ``_check_deployed_files_present``
    already reports those, and double-reporting muddies the audit output.
    Symlinked or unresolved claimed files fail closed because deployment
    ownership is rooted in resolved, managed paths. Lockfile entries without a
    recorded hash (e.g. directories) are skipped silently.
    """
    from ..security.file_scanner import scan_project_result

    # Reuse the already-parsed lock and union its recorded paths with the
    # independently governed deploy-tree scope. The scanner owns exact path
    # accounting and preserves lockfile findings outside resolved targets.
    scan = scan_project_result(
        project_root,
        lockfile=lock,
        include_deployed_trees=True,
        targets=targets,
        user_scope=user_scope,
    )
    findings_by_file = scan.findings_by_file

    # Only critical findings fail this check
    critical_files: list[str] = []
    for rel_path, findings in findings_by_file.items():
        if any(f.severity == "critical" for f in findings):
            critical_files.append(rel_path)
    critical_findings = tuple(
        finding
        for findings in findings_by_file.values()
        for finding in findings
        if finding.severity == "critical"
    )

    from ..core.deployment_ledger import DeploymentLedgerCodec

    ledger = DeploymentLedgerCodec.from_lockfile(lock)
    ledger_values = {
        record.locator.value
        for record in ledger.records.values()
        if record.owners and record.active_owner
    }
    legacy_hash_paths = set(lock.local_deployed_file_hashes)
    for dependency in lock.dependencies.values():
        legacy_hash_paths.update(dependency.deployed_file_hashes)
    missing_ownership = sorted(legacy_hash_paths.difference(ledger_values))

    hash_mismatches, unresolved_hash_paths = verify_deployed_file_hashes(
        project_root, lock, targets
    )
    staged_drift = lock.staged_hash_drift(project_root)
    local_drift = _local_content_drift(project_root, lock)

    if (
        not critical_files
        and not hash_mismatches
        and not staged_drift
        and not local_drift
        and not missing_ownership
        and not unresolved_hash_paths
        and not scan.incomplete
    ):
        return CheckResult(
            name="content-integrity",
            passed=True,
            message="No critical hidden Unicode or hash drift detected",
            coverage=scan.inventory,
        )

    details: list[str] = []
    for entry in scan.incomplete:
        details.append(f"incomplete-coverage: {entry.location}: {entry.diagnostic}")
    details.extend(dict.fromkeys(f"unicode: {finding.location}" for finding in critical_findings))
    for rel_path in missing_ownership:
        details.append(f"missing-ownership: {rel_path}")
    for rel_path in unresolved_hash_paths:
        details.append(f"unresolved: {rel_path}")
    for dep_key, rel_path, expected, actual in (
        (drift.owner, drift.path, drift.expected, drift.actual) for drift in hash_mismatches
    ):
        # Truncate hashes for terminal width; full hashes available via JSON output.
        exp_short = expected.split(":", 1)[-1][:12] if ":" in expected else expected[:12]
        act_short = actual.split(":", 1)[-1][:12] if ":" in actual else actual[:12]
        # Render the synthesized self-entry with a friendly label rather
        # than the internal _SELF_KEY constant ("." is opaque to users).
        dep_label = "<self>" if dep_key == _SELF_KEY else dep_key
        details.append(
            f"hash-drift: {rel_path} (dep={dep_label}, expected={exp_short}..., actual={act_short}...)"
        )
    for rel_path, expected, actual in staged_drift:
        exp_short = expected.split(":", 1)[-1][:12]
        act_short = actual.split(":", 1)[-1][:12]
        details.append(
            f"hash-drift: {rel_path} (staged, expected={exp_short}..., actual={act_short}...)"
        )

    parts: list[str] = []
    remedies: list[str] = []
    remediations: list = []
    if scan.incomplete:
        parts.append(f"{len(scan.incomplete)} primitive(s) with incomplete prompt coverage")
        remedies.append("review the reported format or file access and rerun audit")
    if critical_files:
        parts.append(f"{len(critical_files)} file(s) with critical hidden Unicode")
        remedies.append(
            "review structured prompt fields manually"
            if scan.protected_files.intersection(critical_files)
            else remediation.HIDDEN_CHARACTERS_FLAGGED.clause()
        )
    if hash_mismatches:
        parts.append(f"{len(hash_mismatches)} file(s) with hash drift")
        from ..deps.file_provenance import LOCAL_BUNDLE_OWNER
        from ..install.local_edits import edited_file_remediations

        # Never destructive (RA-044): see install/local_edits.py.
        for bundle in sorted({drift.owner == LOCAL_BUNDLE_OWNER for drift in hash_mismatches}):
            choices = edited_file_remediations(bundle=bundle, user_scope=user_scope)
            remediations.extend(choices)
            remedies.append(remediation.either(*choices))
    if local_drift:
        # install --frozen refuses the same local dependencies (#4447).
        parts.extend(local_drift)
        remedies.append(
            "restore the locked local content, or "
            + remediation.FROZEN_LOCAL_CONTENT_CHANGED.clause()
        )
    if staged_drift:
        parts.append(f"{len(staged_drift)} staged file(s) with hash drift")
        remedies.append(remediation.STAGED_BUNDLE_DRIFT.clause())
    if missing_ownership:
        parts.append(f"{len(missing_ownership)} file(s) without deployment ownership")
        remedies.append(remediation.DEPLOYMENT_OWNERSHIP_MISSING.clause())
    if unresolved_hash_paths:
        parts.append(f"{len(unresolved_hash_paths)} unsafe or unresolved deployed path(s)")
        remedies.append(remediation.MANAGED_PATHS_UNRESOLVED.clause())
    summary = "; ".join(parts)
    remedy = "; ".join(remedies)
    return CheckResult(
        name="content-integrity",
        passed=False,
        message=f"{summary} -- {remedy}",
        details=details,
        coverage=scan.inventory,
        content_findings=critical_findings,
        remediations=tuple(remediations),
    )


def _check_includes_consent(
    manifest: APMPackage,
    lock: LockFile,
) -> CheckResult:
    """Advisory check: nudge toward declaring 'includes:' when local content is deployed.

    This check never hard-fails -- it always returns ``passed=True``.  When
    the lockfile records local content but the manifest does not declare an
    ``includes:`` field, the result message advises the maintainer to add
    ``includes: auto`` (or an explicit list) for governance clarity.  The
    ``[+]`` rendered by the CI table is intentional: this is informational,
    not a violation.  Use ``manifest.require_explicit_includes`` policy to
    promote this to a hard block.
    """
    if not lock.local_deployed_files:
        return CheckResult(
            name="includes-consent",
            passed=True,
            message="No local content deployed -- includes consent check skipped",
        )

    if manifest.includes is None:
        return CheckResult(
            name="includes-consent",
            passed=True,
            message=(
                "Local content deployed but 'includes:' not declared in "
                "apm.yml -- consider adding 'includes: auto' for explicit consent"
            ),
        )

    return CheckResult(
        name="includes-consent",
        passed=True,
        message="'includes:' declared -- local content deployment is explicitly consented",
    )


#: Prefix used in the drift :class:`CheckResult` message when the check is
#: skipped due to a cold cache.  ``audit.py`` imports this to detect the
#: skip case without comparing against a raw string literal.
DRIFT_SKIP_PREFIX = "drift skipped"


def _without_pre037_renders(
    findings: list[DriftFinding], lockfile: LockFile, deployment_root: Path
) -> list[DriftFinding]:
    """Drop rendering changes of files an APM before 0.37 deployed (#4398).

    ``install --frozen`` adopts such a file as committed and a plain install
    migrates it once, so audit agrees: it is not drift.
    """
    from ..core.legacy_lock_evidence import legacy_bytes_owned, legacy_lock_hashes

    recorded = legacy_lock_hashes(lockfile)
    if not recorded:
        return findings
    return [
        finding
        for finding in findings
        if not (
            finding.kind == "modified"
            and legacy_bytes_owned(deployment_root / finding.path, recorded.get(finding.path))
        )
    ]


def _check_drift(
    project_root: Path,
    lockfile: LockFile,
    targets: Sequence[str] | None = None,
    cache_only: bool = True,
    verbose: bool = False,
    prepared_replay: PreparedCiAuditReplay | None = None,
    policy_provider: Callable[[], Any] | None = None,
    policy_fetch: Callable[[], Any] | None = None,
) -> tuple[CheckResult, list[DriftFinding]]:
    """Replay the install in a scratch dir and diff against the project.

    Returns the standard :class:`CheckResult` PLUS the list of
    :class:`DriftFinding` instances so callers can render them in the
    output format of their choice (text/json/sarif) without re-running
    the replay.

    Cache-only by default: a missing cache entry skips the check with
    an informational message rather than failing it.  Drift can only
    run once the local cache has been warmed by ``apm install``; until
    then the audit remains non-blocking so CI does not red-mark a
    fresh checkout that has never installed.
    """
    from ..agent_plugins.errors import AgentPluginDeploymentBoundaryError
    from ..codex_plugins.client import CodexNativeError
    from ..core.scope import get_workspace_deploy_root
    from ..deps.lockfile import get_lockfile_path
    from ..deps.path_anchoring import LocalResolutionError
    from ..install.audit_target_roots import external_replay_root
    from ..install.drift import (
        CacheMissError,
        CheckLogger,
        ReplayConfig,
        diff_scratch_against_project,
        run_replay,
    )
    from ..install.errors import PolicyViolationError
    from ..install.phases.local_content import LocalPackageError
    from ..integration.targets import resolve_audit_targets

    logger = CheckLogger(verbose=verbose)
    deployment_root = get_workspace_deploy_root(project_root)
    user_scope = deployment_root != project_root.resolve()
    if prepared_replay is None:
        config = ReplayConfig(
            project_root=project_root,
            lockfile_path=get_lockfile_path(project_root),
            targets=frozenset(targets) if targets else None,
            cache_only=cache_only,
            user_scope=user_scope,
            policy_provider=policy_provider,
            policy_fetch=policy_fetch,
        )

        try:
            scratch = run_replay(config, logger)
        except (AgentPluginDeploymentBoundaryError, CodexNativeError) as exc:
            return (
                CheckResult(
                    name="drift",
                    passed=False,
                    message=f"drift replay blocked: {exc}",
                    details=[str(exc)],
                ),
                [],
            )
        except PolicyViolationError as exc:
            # Org policy asked to block when it cannot be resolved.
            return (
                CheckResult(
                    name="drift",
                    passed=False,
                    message=f"drift replay blocked: {exc}",
                    details=[str(exc)],
                ),
                [],
            )
        except LocalResolutionError as exc:
            return (
                CheckResult(
                    name="drift",
                    passed=False,
                    message=(
                        f"drift replay failed: corrupt local dependency graph in the "
                        f"lockfile ({exc}). Fix the resolved_by chain, or "
                        f"{remediation.LOCAL_DEPENDENCY_GRAPH_CORRUPT.clause()}."
                    ),
                ),
                [],
            )
        except LocalPackageError as exc:
            # A failed check, not an uncaught error: JSON and SARIF output
            # still carry one document with the failure (#4287).
            return (
                CheckResult(
                    name="drift",
                    passed=False,
                    message=f"drift replay failed: {exc}",
                    details=[str(exc)],
                ),
                [],
            )
        except CacheMissError:
            return (
                CheckResult(
                    name="drift",
                    passed=True,
                    message=(
                        f"{DRIFT_SKIP_PREFIX}: install cache not populated "
                        f"({remediation.DRIFT_CACHE_UNPOPULATED.clause()}, or pass --no-drift)"
                    ),
                ),
                [],
            )
        except NotImplementedError as exc:
            return (
                CheckResult(
                    name="drift",
                    passed=False,
                    message=f"drift replay unsupported: {exc}",
                ),
                [],
            )
        resolved_targets = resolve_audit_targets(
            project_root,
            user_scope=user_scope,
            lockfile=lockfile,
        )
        tracked_files = None
    else:
        scratch = prepared_replay.scratch_root
        resolved_targets = list(prepared_replay.targets)
        tracked_files = prepared_replay.tracked_files

    logger.diff_start()
    if targets:
        resolved_targets = [t for t in resolved_targets if t.name in set(targets)]
    project_targets = [target for target in resolved_targets if target.managed_deploy_root is None]
    findings = diff_scratch_against_project(
        scratch,
        deployment_root,
        lockfile,
        project_targets,
        tracked_files=tracked_files,
    )
    for target in resolved_targets:
        live_root = target.managed_deploy_root
        if live_root is None:
            continue
        from ..install.audit_target_roots import external_target_relative_roots

        governed_roots = external_target_relative_roots(target)
        if not governed_roots:
            continue
        comparison_target = replace(target, root_dir=".", resolved_deploy_root=None)
        external_findings = diff_scratch_against_project(
            external_replay_root(scratch, target),
            live_root,
            lockfile,
            [comparison_target],
            absolute_claims_only=True,
            governed_roots=governed_roots,
        )
        findings.extend(
            replace(finding, path=str(live_root / finding.path)) for finding in external_findings
        )

    findings = _without_pre037_renders(findings, lockfile, deployment_root)
    if not findings:
        logger.clean()
        return (
            CheckResult(
                name="drift",
                passed=True,
                message="no drift detected against lockfile",
            ),
            [],
        )

    logger.findings(len(findings))
    preview = ", ".join(f.path for f in findings[:3])
    suffix = "" if len(findings) <= 3 else f" (+{len(findings) - 3} more)"
    return (
        CheckResult(
            name="drift",
            passed=False,
            message=f"drift detected: {len(findings)} file(s): {preview}{suffix}",
            details=[f"{f.kind}: {f.path}" for f in findings],
        ),
        findings,
    )


# -- Aggregate runner ----------------------------------------------


def _bundle_hook_ownership_result(problems: Sequence[str]) -> CheckResult:
    """Bundle hook entries APM can no longer attribute are named, never dropped (#5078)."""
    return CheckResult(
        name="local-bundle-hooks",
        passed=False,
        message=(
            f"{len(problems)} local bundle hook ownership problem(s); restore each recorded "
            "entry and ownership record first (uninstall refuses while an edited entry still "
            "runs the bundle's script), and keep your own changes in a separate hook entry"
        ),
        details=list(problems),
    )


def _check_local_bundle_hooks(
    deployment_root: Path,
    lock: LockFile,
    manifest: APMPackage,
    targets: Sequence[TargetProfile],
    *,
    user_scope: bool,
    no_policy: bool,
) -> CheckResult:
    """Fail when a deployed local-bundle hook or canvas extension's approval no longer holds."""
    from ..core.deployment_ledger import DeploymentLedgerCodec
    from ..install.errors import PolicyViolationError
    from ..install.local_bundle_canvas import unadmitted_bundle_canvas
    from ..install.local_bundle_hook_trust import (
        bundle_hook_ownership,
        bundle_hook_recheck_targets,
        read_bundle_hook_states,
        unadmitted_bundle_hooks,
    )
    from ..security.executables import resolve_project_exec_allow_map

    # Hooks a bundle registered for a target the manifest does not name are
    # re-checked too: every hook target of the scope.
    targets = bundle_hook_recheck_targets(targets, root=deployment_root, user_scope=user_scope)

    def allow_provider():
        return resolve_project_exec_allow_map(
            deployment_root, manifest, user_scope=user_scope, no_policy=no_policy
        )

    bundle_paths = DeploymentLedgerCodec.local_bundle_values(
        DeploymentLedgerCodec.from_lockfile(lock)
    )
    try:
        # Each hook config, sidecar and bundle record is read once per audit (#5211).
        states = read_bundle_hook_states(deployment_root, targets)
        problems = bundle_hook_ownership(deployment_root, targets, bundle_paths, states)
        unadmitted = unadmitted_bundle_hooks(
            deployment_root, targets, allow_provider, bundle_paths, states
        )
        canvas = unadmitted_bundle_canvas(
            deployment_root, targets, allow_provider, bundle_paths, states.read
        )
    except PolicyViolationError as exc:
        return CheckResult(name="local-bundle-hooks", passed=False, message=str(exc))
    if problems:
        return _bundle_hook_ownership_result(problems)
    if not unadmitted and not canvas:
        return CheckResult(
            name="local-bundle-hooks", passed=True, message="Local bundle hooks are approved"
        )
    counts = [
        f"{len(items)} local bundle(s) have {kind} the current executables trust does not admit"
        for kind, items in (("hooks", unadmitted), ("canvas extensions", canvas))
        if items
    ]
    return CheckResult(
        name="local-bundle-hooks",
        passed=False,
        message=(
            f"{'; '.join(counts)}; {remediation.BUNDLE_HOOKS_UNADMITTED.clause()}, "
            f"or {remediation.BUNDLE_HOOKS_APPROVED.clause()}"
        ),
        details=[
            *(f"{item.slug}: {item.reason}" for item in unadmitted),
            *(f"{item.slug}: canvas: {item.reason}" for item in canvas),
        ],
    )


class _PolicyRouteBlocks(Exception):
    """The audit's policy-outcome route blocks on its one discovery."""


def _audit_trust_provider(
    project_root: Path,
    manifest: APMPackage,
    user_scope: bool,
    policy_fetch: Callable[[], Any] | None,
    fallback: Callable[[], Any],
) -> Callable[[], Any]:
    """The executables trust context judged on the audit's own policy discovery.

    *policy_fetch* is the one discovery ``apm audit --ci`` routes its policy
    outcome from; it is classified exactly as that route classifies it, so a
    deferral here always coincides with the route failing the audit (#4821).
    Without it (no audit-level discovery) *fallback* resolves the context.
    """
    resolved: list[Any] = []

    def provide() -> Any:
        if resolved:
            return resolved[0]
        if policy_fetch is None:
            resolved.append(fallback())
            return resolved[0]
        from ..core.scope import scope_manifest_path
        from ..install.exec_gate import trust_context_for_policy
        from .outcome_routing import classify_discovery_outcome
        from .project_config import read_project_fetch_failure_default

        route = classify_discovery_outcome(
            policy_fetch(),
            fetch_failure_default=read_project_fetch_failure_default(project_root),
        )
        if route.blocks:
            raise _PolicyRouteBlocks(route.outcome)
        resolved.append(
            trust_context_for_policy(
                scope_manifest_path(project_root, user_scope=user_scope),
                route.policy,
                getattr(manifest, "allow_executables", None),
            )
        )
        return resolved[0]

    return provide


def _check_hook_approval_pins(
    project_root: Path,
    deployment_root: Path,
    lock: LockFile,
    manifest: APMPackage,
    *,
    user_scope: bool,
    no_policy: bool,
    prepared_replay: PreparedCiAuditReplay | None,
    targets: Sequence[TargetProfile] = (),
    policy_fetch: Callable[[], Any] | None = None,
) -> CheckResult:
    """Fail when a locked hook file's pinned approval no longer admits it (#4188).

    Also fails when a file the lock does not record sits at the hook path of
    a package whose hooks the gate withholds (a denied or unapproved
    package): install keeps such a file and announces it, never deletes it.

    Routes through the owner ``apm install --frozen`` uses, so both commands
    reach one verdict with one remediation.
    """
    from ..constants import APM_MODULES_DIR
    from ..install.errors import PolicyViolationError
    from ..install.exec_gate import locked_hook_pin_mismatches
    from ..install.withheld_hooks import locked_withheld_hook_files
    from ..install.withheld_merged_hooks import ENTRY_REASON
    from ..security.executables import resolve_project_exec_allow_map

    modules_root = (
        prepared_replay.modules_root
        if prepared_replay is not None
        else project_root / APM_MODULES_DIR
    )

    resolved: list[Any] = []

    def trust_ctx() -> Any:
        # Resolved once, and only when a locked package declares executables.
        if not resolved:
            allow = resolve_project_exec_allow_map(
                deployment_root, manifest, user_scope=user_scope, no_policy=no_policy
            )
            resolved.append(getattr(allow, "trust_context", None))
        return resolved[0]

    try:
        mismatches = locked_hook_pin_mismatches(lock, modules_root, trust_ctx)
    except PolicyViolationError as exc:
        return CheckResult(name="hook-approval-pins", passed=False, message=str(exc))
    try:
        withheld = locked_withheld_hook_files(
            lock,
            modules_root,
            _audit_trust_provider(project_root, manifest, user_scope, policy_fetch, trust_ctx),
            deployment_root,
            targets,
            user_scope=user_scope,
        )
    except _PolicyRouteBlocks:
        # The audit's own policy route blocks on this same discovery and fails,
        # reporting the cause once; this check does not repeat it (#4821).
        withheld = []
    except PolicyViolationError as exc:
        return CheckResult(name="hook-approval-pins", passed=False, message=str(exc))
    if withheld:
        entries = [row for row in withheld if row[0] and ENTRY_REASON in row[2]]
        files = [row for row in withheld if row[0] and ENTRY_REASON not in row[2]]
        unchecked = [row for row in withheld if not row[0]]
        message = []
        if entries:
            message.append(
                f"{len(entries)} merged hook entr(ies) match the hooks of a package the "
                "executables gate withholds, and no ownership record attributes them -- "
                f"{remediation.WITHHELD_HOOK_ENTRY_UNRECORDED.clause()}"
            )
        if files:
            message.append(
                f"{len(files)} file(s) sit at the hook path of a package whose hooks the "
                "executables gate withholds, and apm.lock.yaml does not record them -- "
                f"{remediation.WITHHELD_HOOK_FILE_UNRECORDED.clause()}"
            )
        if unchecked:
            message.append(
                f"{len(unchecked)} withheld package(s) whose hook paths cannot be checked -- "
                f"{remediation.WITHHELD_HOOK_PATHS_UNCHECKED.clause()}"
            )
        return CheckResult(
            name="hook-approval-pins",
            passed=False,
            message="; ".join(message),
            details=[f"{path}: hook entry of {key} ({reason})" for path, key, reason in entries]
            + [
                f"{path}: hook path of {key} ({reason}); not recorded in apm.lock.yaml"
                for path, key, reason in files
            ]
            + [f"{key}: {reason}" for _, key, reason in unchecked]
            + [mismatch.describe() for mismatch in mismatches],
        )
    if not mismatches:
        return CheckResult(
            name="hook-approval-pins",
            passed=True,
            message="Deployed hooks match their approval pins",
        )
    return CheckResult(
        name="hook-approval-pins",
        passed=False,
        message=(
            f"{len(mismatches)} hook file(s) recorded in apm.lock.yaml are no longer "
            "admitted by their approval"
        ),
        details=[mismatch.describe() for mismatch in mismatches],
    )


def run_baseline_checks(
    project_root: Path,
    *,
    fail_fast: bool = True,
    ci_mode: bool = False,
    prepared_replay: PreparedCiAuditReplay | None = None,
    prepared_replay_error: str | None = None,
    replay_error_owner: str | None = None,
    no_policy: bool = False,
    policy_provider: Callable[[], Any] | None = None,
    policy_fetch: Callable[[], Any] | None = None,
    content_integrity: bool = True,
    mcp_config_consistency: bool = True,
) -> CIAuditResult:
    """Run all baseline CI checks against a project directory.

    *content_integrity* ``False`` skips the deployed-content scan; only a caller
    that has itself proven every deployed byte (hash and scan) may pass it.
    *mcp_config_consistency* ``False`` skips the MCP config-consistency check
    (Check 7; 7b and 7c still run); only a caller that then runs the install's
    own MCP phase on the same state (which reconciles and records exactly as
    install does, honoring personal approvals the audit view ignores) may pass it.

    When *fail_fast* is ``True`` (default), stops after the first
    failing check to skip expensive I/O (e.g. content integrity scan).
    When *ci_mode* is ``True``, a missing ``apm.yml`` is always a hard
    ``manifest-missing`` failure, so a CI gate never passes without auditing
    anything; otherwise it is an advisory warning only when APM artifacts exist.
    *replay_error_owner* names the later check that reports *prepared_replay_error*
    in full (``drift``); without it the first check that needs the replay does.
    *policy_fetch* is the one org policy discovery ``apm audit --ci`` routes
    its policy outcome from, so checks that judge trust use the same result.
    Returns :class:`CIAuditResult` with individual check results.
    """
    from ..constants import APM_MODULES_DIR
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ._shared import _parse_apm_yml_safe

    result = CIAuditResult()
    lockfile_path = get_lockfile_path(project_root)
    lock, unreadable = _read_audited_lock(lockfile_path)
    deployment_root, user_scope, resolved_targets = resolve_audit_scope(project_root, lock)
    apm_yml_path = manifest_file(project_root)

    # Parse manifest ONCE -- this function owns parse-error handling.
    manifest: APMPackage | None = None
    if apm_yml_path.exists():
        manifest = cast("APMPackage | None", _parse_apm_yml_safe(apm_yml_path, result))
        if manifest is None:
            return result

    # Check 1: Lockfile exists and is readable (manifest already parsed, pass it in)
    result.checks.append(unreadable or _check_lockfile_exists(project_root, manifest))

    # If lockfile doesn't exist or isn't needed, remaining checks can't run
    if not result.checks[0].passed:
        return result

    # If there's no apm.yml or no lockfile, the first check already passed
    # (no deps needed).  Skip remaining checks -- but warn if APM artifacts
    # exist without a manifest (evidence of a deleted apm.yml).
    if not apm_yml_path.exists() or not lockfile_path.exists():
        if not apm_yml_path.exists():
            apm_dir = project_root / ".apm"
            lock_file = project_root / LOCKFILE_NAME
            legacy_lock_file = project_root / LEGACY_LOCKFILE_NAME
            if apm_dir.is_dir() or lock_file.exists() or legacy_lock_file.exists():
                result.checks.append(
                    CheckResult(
                        name="manifest-missing",
                        passed=not ci_mode,
                        message=(
                            "apm.yml is missing but APM artifacts"
                            " (.apm/ or apm.lock.yaml or apm.lock) were found"
                            " -- this may indicate a deleted manifest. "
                            f"{remediation.PROJECT_MANIFEST_MISSING.sentence()}"
                        ),
                        remediations=(remediation.PROJECT_MANIFEST_MISSING,),
                    )
                )
            elif ci_mode:
                # A CI gate that checks nothing must not pass: the usual cause
                # is running from a subdirectory or the wrong checkout path.
                result.checks.append(
                    CheckResult(
                        name="manifest-missing",
                        passed=False,
                        message=(
                            "No apm.yml in the current directory -- nothing was audited."
                            " Run 'apm audit --ci' from the project root that"
                            " contains apm.yml (APM does not search parent directories)."
                        ),
                    )
                )
        result.checks.append(
            _check_content_integrity(
                deployment_root, LockFile(), resolved_targets, user_scope=user_scope
            )
        )
        result.checks.extend(
            _kept_mcp_entries(deployment_root, LockFile(), user_scope=user_scope, lockless=True)
        )
        return result

    if lock is None or manifest is None:
        return result

    def _run(check: CheckResult) -> bool:
        """Append check and return True if fail-fast should stop."""
        result.checks.append(check)
        return fail_fast and not check.passed

    # Check 2: Ref consistency (external manifest <-> lockfile identity)
    #
    # External-boundary identity checks MUST surface before internal
    # reconciliation checks (Check 3, deployment-ledger-owners) because the
    # latter's remedy assumes the lockfile's identity claims are legitimate.
    # A source-identity tamper (attacker rewrites a dependency's host/repo_url)
    # trips both: ref-consistency ("not found in lockfile" -> re-resolve from the
    # trusted manifest via 'apm install --update') and deployment-ledger-owners
    # (stale owner -> 'apm prune'). Under fail-fast the first failing check wins,
    # and 'apm prune' on a tampered lockfile would reconcile ownership toward the
    # attacker source, so ref-consistency -- the safe, manifest-driven remedy --
    # must be evaluated first. The ledger-owner check still owns the genuine
    # departed-owner case (req-pl-016), where ref-consistency passes.
    if _run(_check_ref_consistency(manifest, lock)):
        return result

    # Check 3: Canonical deployment owner references
    if _run(_check_deployment_ledger_owners(lock)):
        return result

    claim_targets = resolve_claim_targets(
        project_root, lock, user_scope=user_scope, targets=resolved_targets
    )

    # Check 4: Deployed files present
    if _run(_check_deployed_files_present(deployment_root, lock, claim_targets)):
        return result

    # Check 5: No orphaned packages
    # 5b: no files of a target apm.yml dropped (#4517), as --frozen refuses
    if _run(_check_no_orphans(manifest, lock)) or _run(
        _check_lockfile_targets_current(project_root, lock, user_scope=user_scope)
    ):
        return result

    # Check 6: Skill subset consistency (manifest vs lockfile)
    if _run(
        _check_skill_subset_consistency(
            manifest,
            lock,
            project_root,
            prepared_replay=prepared_replay,
            prepared_replay_error=prepared_replay_error,
            replay_error_owner=replay_error_owner,
        )
    ):
        return result

    # Check 7: Config consistency (MCP); 7b: a differing same-name native MCP
    # entry --frozen refuses (#4477)
    if (
        (
            mcp_config_consistency
            and _run(
                _check_config_consistency(
                    manifest,
                    lock,
                    prepared_replay=prepared_replay,
                    prepared_replay_error=prepared_replay_error,
                    replay_error_owner=replay_error_owner,
                    no_policy=no_policy,
                )
            )
        )
        or _run(
            _check_native_mcp_ownership(project_root, lock, resolved_targets, user_scope=user_scope)
        )
        # 7c: recorded native MCP entries edited since APM wrote them (#4757)
        or any(
            _run(check)
            for check in _edited_native_mcp_entries(
                project_root,
                lock,
                resolved_targets,
                user_scope=user_scope,
                # The scratch replay failed: config-consistency reports it.
                plugin_roots=None
                if prepared_replay_error is not None
                else lambda: _native_mcp_plugin_roots(
                    project_root,
                    manifest,
                    lock,
                    prepared_replay=prepared_replay,
                    no_policy=no_policy,
                ),
            )
        )
        # 7d: MCP entries kept without a lockfile because they could not be proven (#4737)
        or any(
            _run(check) for check in _kept_mcp_entries(deployment_root, lock, user_scope=user_scope)
        )
    ):
        return result

    # Check 8: Content integrity; 8b: local-bundle hooks still admitted (#4027)
    if (
        _run(
            _check_content_integrity(deployment_root, lock, claim_targets, user_scope=user_scope)
            if content_integrity
            else CheckResult(name="content-integrity", passed=True, message="proven by the caller")
        )
        or _run(
            _check_local_bundle_hooks(
                deployment_root,
                lock,
                manifest,
                resolved_targets,
                user_scope=user_scope,
                no_policy=no_policy,
            )
        )
        or _run(
            # 8c: locked hooks still admitted by their approval pins (#4188),
            # and no unrecorded file at a withheld package's hook path
            _check_hook_approval_pins(
                project_root,
                deployment_root,
                lock,
                manifest,
                user_scope=user_scope,
                no_policy=no_policy,
                prepared_replay=prepared_replay,
                targets=resolved_targets,
                policy_fetch=policy_fetch,
            )
        )
        or _run(
            # 8d: recorded executables decisions are still the gate's (#4053),
            # after the pins, so audit and --frozen name the same first cause.
            _check_exec_status_current(
                project_root,
                lock,
                manifest,
                resolved_targets,
                user_scope=user_scope,
                modules_root=(
                    prepared_replay.modules_root
                    if prepared_replay is not None
                    else project_root / APM_MODULES_DIR
                ),
                policy_provider=policy_provider,
            )
        )
    ):
        return result

    # Check 9: Includes consent (advisory; never hard-fails)
    _run(_check_includes_consent(manifest, lock))

    return result
