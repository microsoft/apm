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
from pathlib import Path
from typing import TYPE_CHECKING, Sequence  # noqa: UP035

from ..core.deployment_ledger import DEPLOYMENT_OWNER_REMEDIATION
from ..deps.lockfile import _SELF_KEY, LEGACY_LOCKFILE_NAME, LOCKFILE_NAME
from .models import CheckResult, CIAuditResult

if TYPE_CHECKING:
    from ..deps.lockfile import LockFile
    from ..install.drift import DriftFinding

_logger = logging.getLogger(__name__)


# -- Individual checks ---------------------------------------------


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

    has_deps = manifest.has_any_apm_dependencies() or bool(manifest.get_all_mcp_dependencies())
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
        message="Lockfile missing -- run 'apm install' to generate apm.lock.yaml",
        details=["apm.yml declares dependencies but apm.lock.yaml is absent"],
    )


def _check_ref_consistency(
    manifest: APMPackage,
    lock: LockFile,
) -> CheckResult:
    """Verify manifest refs match the lockfile's resolved ref and commit."""
    from ..deps.revision_pins import is_full_revision_pin
    from ..drift import detect_ref_change

    mismatches: list[str] = []
    requires_update = False
    for dep_ref in manifest.get_all_apm_dependencies():
        key = dep_ref.get_unique_key()
        locked_dep = lock.get_dependency(key)
        if locked_dep is None:
            mismatches.append(f"{key}: not found in lockfile")
            requires_update = requires_update or bool(lock.dependencies)
            continue
        if is_full_revision_pin(dep_ref.reference) and (
            locked_dep.resolved_commit != dep_ref.reference
        ):
            mismatches.append(
                f"{key}: manifest commit '{dep_ref.reference}' != "
                f"lockfile resolved_commit '{locked_dep.resolved_commit or '(missing)'}'"
            )
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
    repair_command = "apm install --update" if requires_update else "apm install"
    return CheckResult(
        name="ref-consistency",
        passed=False,
        message=(
            f"{len(mismatches)} ref mismatch(es) -- run '{repair_command}' to update lockfile"
        ),
        details=mismatches,
    )


def _check_deployed_files_present(
    project_root: Path,
    lock: LockFile,
) -> CheckResult:
    """Verify all files listed in lockfile deployed_files exist on disk."""
    from ..integration.base_integrator import BaseIntegrator

    missing: list[str] = []
    for _dep_key, dep in lock.dependencies.items():
        for rel_path in dep.deployed_files:
            safe_path = rel_path.rstrip("/")
            if not BaseIntegrator.validate_deploy_path(safe_path, project_root):
                continue  # skip unsafe paths silently
            abs_path = project_root / rel_path
            if not abs_path.exists():
                missing.append(rel_path)

    if not missing:
        return CheckResult(
            name="deployed-files-present",
            passed=True,
            message="All deployed files present on disk",
        )
    return CheckResult(
        name="deployed-files-present",
        passed=False,
        message=(f"{len(missing)} deployed file(s) missing -- run 'apm install' to restore"),
        details=missing,
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
            f"{len(violations)} invalid deployment ownership record(s) - "
            f"{DEPLOYMENT_OWNER_REMEDIATION}"
        ),
        details=details,
    )


def _check_no_orphans(
    manifest: APMPackage,
    lock: LockFile,
) -> CheckResult:
    """Verify no packages in lockfile are absent from manifest.

    Only DIRECT dependencies (``depth == 1`` / no ``resolved_by``) are
    candidates for orphan detection. Transitive deps belong to a
    sub-package's manifest, not the root manifest, so the root manifest
    cannot make them go away by editing its dependency lists.
    """
    manifest_keys = {dep.get_unique_key() for dep in manifest.get_all_apm_dependencies()}
    orphaned = [
        dep_key
        for dep_key, locked_dep in lock.dependencies.items()
        if dep_key not in manifest_keys and dep_key != _SELF_KEY and locked_dep.resolved_by is None
    ]
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
            f"{len(orphaned)} orphaned package(s) in lockfile -- run 'apm install' to clean up"
        ),
        details=orphaned,
    )


def _check_skill_subset_consistency(
    manifest: APMPackage,
    lock: LockFile,
) -> CheckResult:
    """Verify lockfile skill_subset matches manifest skills: for each entry."""
    mismatches: list[str] = []
    for dep_ref in manifest.get_all_apm_dependencies():
        key = dep_ref.get_unique_key()
        locked_dep = lock.get_dependency(key)
        if locked_dep is None:
            continue
        # Only check skill_bundle packages
        if locked_dep.package_type != "skill_bundle":
            continue
        manifest_subset = sorted(dep_ref.skill_subset) if dep_ref.skill_subset else []
        lock_subset = sorted(locked_dep.skill_subset) if locked_dep.skill_subset else []
        if manifest_subset != lock_subset:
            mismatches.append(
                f"{key}: manifest skills {manifest_subset} != lockfile skill_subset {lock_subset}"
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


def _check_config_consistency(
    manifest: APMPackage,
    lock: LockFile,
) -> CheckResult:
    """Verify MCP server configs match lockfile baseline."""
    from ..constants import APM_MODULES_DIR
    from ..integration.mcp_config_view import CurrentMcpConfigView

    project_root = manifest.package_path or Path.cwd()
    view = CurrentMcpConfigView.derive(
        manifest,
        lock,
        project_root / APM_MODULES_DIR,
        trust_transitive_self_defined=True,
    )
    stored_configs = lock.mcp_configs or {}
    diff = view.diff(stored_configs)

    # No MCP deps at all -- nothing to check
    if not view.configs and not stored_configs and not view.problems:
        return CheckResult(
            name="config-consistency",
            passed=True,
            message="No MCP configs to check",
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
        return CheckResult(
            name="config-consistency",
            passed=True,
            message="MCP configs match lockfile baseline",
        )
    return CheckResult(
        name="config-consistency",
        passed=False,
        message=(f"{len(details)} MCP config inconsistenc(ies) -- run 'apm install' to reconcile"),
        details=details,
    )


def _check_content_integrity(
    project_root: Path,
    lock: LockFile,
) -> CheckResult:
    """Check deployed files for critical hidden Unicode and hash drift.

    Two signals are evaluated:
      * Critical hidden Unicode (steganographic markers) via the file
        scanner.
      * SHA-256 drift between the on-disk content and the canonical deployment
        ledger hash recorded at install time.
      * Missing canonical ownership metadata for a legacy deployed-file hash.

    Missing files are deliberately skipped here -- ``_check_deployed_files_present``
    already reports those, and double-reporting muddies the audit output.
    Symlinks are skipped because they may legitimately point elsewhere,
    and lockfile entries without a recorded hash (e.g. directories) are
    skipped silently.
    """
    from ..security.file_scanner import scan_lockfile_packages
    from ..utils.content_hash import compute_file_hash

    findings_by_file, _files_scanned = scan_lockfile_packages(project_root)

    # Only critical findings fail this check
    critical_files: list[str] = []
    for rel_path, findings in findings_by_file.items():
        if any(f.severity == "critical" for f in findings):
            critical_files.append(rel_path)

    from ..core.deployment_ledger import DeploymentLedgerCodec
    from ..core.deployment_state import LocatorKind
    from ..integration.targets import KNOWN_TARGETS

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

    # Per-file hash verification across canonical deployment records.
    hash_mismatches: list[tuple] = []  # (dep_key, rel_path, expected, actual)
    # Local import: matches the scoping pattern used in
    # _check_deployed_files_present (line 131); avoids cycles.
    from ..integration.base_integrator import BaseIntegrator as _BaseIntegrator

    for record in ledger.records.values():
        expected_hash = record.content_hash
        if expected_hash is None:
            continue
        locator = record.locator
        if locator.kind == LocatorKind.URI:
            continue
        if locator.kind == LocatorKind.PROJECT_RELATIVE:
            safe_rel = locator.value.rstrip("/")
            if not _BaseIntegrator.validate_deploy_path(safe_rel, project_root):
                continue
            file_path = project_root / safe_rel
        else:
            target = KNOWN_TARGETS.get(locator.target)
            if target is None:
                continue
            try:
                resolved = DeploymentLedgerCodec.resolve_locator(
                    locator,
                    project_root=project_root,
                    target=target,
                )
            except (OSError, RuntimeError, ValueError):
                continue
            if isinstance(resolved, str):
                continue
            file_path = resolved
        if not file_path.exists():
            continue  # _check_deployed_files_present owns this signal
        if file_path.is_symlink() or not file_path.is_file():
            continue
        actual_hash = compute_file_hash(file_path)
        if actual_hash != expected_hash:
            hash_mismatches.append((record.active_owner, locator.value, expected_hash, actual_hash))

    if not critical_files and not hash_mismatches and not missing_ownership:
        return CheckResult(
            name="content-integrity",
            passed=True,
            message="No critical hidden Unicode or hash drift detected",
        )

    details: list[str] = []
    for rel_path in critical_files:
        details.append(f"unicode: {rel_path}")
    for rel_path in missing_ownership:
        details.append(f"missing-ownership: {rel_path}")
    for dep_key, rel_path, expected, actual in hash_mismatches:
        # Truncate hashes for terminal width; full hashes available via JSON output.
        exp_short = expected.split(":", 1)[-1][:12] if ":" in expected else expected[:12]
        act_short = actual.split(":", 1)[-1][:12] if ":" in actual else actual[:12]
        # Render the synthesized self-entry with a friendly label rather
        # than the internal _SELF_KEY constant ("." is opaque to users).
        dep_label = "<self>" if dep_key == _SELF_KEY else dep_key
        details.append(
            f"hash-drift: {rel_path} (dep={dep_label}, expected={exp_short}..., actual={act_short}...)"
        )

    parts: list[str] = []
    remedies: list[str] = []
    if critical_files:
        parts.append(f"{len(critical_files)} file(s) with critical hidden Unicode")
        remedies.append("'apm audit --strip' to clean Unicode")
    if hash_mismatches:
        parts.append(f"{len(hash_mismatches)} file(s) with hash drift")
        remedies.append("'apm install' to restore drifted files")
    if missing_ownership:
        parts.append(f"{len(missing_ownership)} file(s) without deployment ownership")
        remedies.append("'apm install' to repair ownership metadata")
    summary = "; ".join(parts)
    remedy = " and ".join(remedies)
    return CheckResult(
        name="content-integrity",
        passed=False,
        message=f"{summary} -- run {remedy}",
        details=details,
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


def _check_drift(
    project_root: Path,
    lockfile: LockFile,
    targets: Sequence[str] | None = None,
    cache_only: bool = True,
    verbose: bool = False,
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
    from ..deps.lockfile import get_lockfile_path
    from ..deps.path_anchoring import LocalResolutionError
    from ..install.drift import (
        CacheMissError,
        CheckLogger,
        ReplayConfig,
        diff_scratch_against_project,
        run_replay,
    )
    from ..integration.targets import resolve_targets

    logger = CheckLogger(verbose=verbose)
    config = ReplayConfig(
        project_root=project_root,
        lockfile_path=get_lockfile_path(project_root),
        targets=frozenset(targets) if targets else None,
        cache_only=cache_only,
    )

    try:
        scratch = run_replay(config, logger)
    except LocalResolutionError as exc:
        return (
            CheckResult(
                name="drift",
                passed=False,
                message=(
                    f"drift replay failed: corrupt local dependency graph in the "
                    f"lockfile ({exc}). Fix the resolved_by chain or re-run 'apm install'."
                ),
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
                    "(run 'apm install' first or pass --no-drift)"
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

    logger.diff_start()
    resolved_targets = resolve_targets(project_root)
    if targets:
        resolved_targets = [t for t in resolved_targets if t.name in set(targets)]
    findings = diff_scratch_against_project(scratch, project_root, lockfile, resolved_targets)

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


def run_baseline_checks(
    project_root: Path,
    *,
    fail_fast: bool = True,
    ci_mode: bool = False,
) -> CIAuditResult:
    """Run all baseline CI checks against a project directory.

    When *fail_fast* is ``True`` (default), stops after the first
    failing check to skip expensive I/O (e.g. content integrity scan).
    When *ci_mode* is ``True``, the ``manifest-missing`` check is a hard
    failure (``passed=False``); otherwise it is an advisory warning only.
    Returns :class:`CIAuditResult` with individual check results.
    """
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ._shared import _parse_apm_yml_safe

    result = CIAuditResult()
    apm_yml_path = project_root / "apm.yml"

    # Parse manifest ONCE -- this function owns parse-error handling.
    manifest = None
    if apm_yml_path.exists():
        manifest = _parse_apm_yml_safe(apm_yml_path, result)
        if manifest is None:
            return result

    # Check 1: Lockfile exists (manifest already parsed, pass it in)
    result.checks.append(_check_lockfile_exists(project_root, manifest))

    # If lockfile doesn't exist or isn't needed, remaining checks can't run
    if not result.checks[0].passed:
        return result

    lockfile_path = get_lockfile_path(project_root)

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
                            " -- this may indicate a deleted manifest"
                        ),
                    )
                )
        return result

    lock = LockFile.read(lockfile_path)
    if lock is None:
        return result

    def _run(check: CheckResult) -> bool:
        """Append check and return True if fail-fast should stop."""
        result.checks.append(check)
        return fail_fast and not check.passed

    # Check 2: Canonical deployment owner references
    if _run(_check_deployment_ledger_owners(lock)):
        return result

    # Check 3: Ref consistency
    if _run(_check_ref_consistency(manifest, lock)):
        return result

    # Check 4: Deployed files present
    if _run(_check_deployed_files_present(project_root, lock)):
        return result

    # Check 5: No orphaned packages
    if _run(_check_no_orphans(manifest, lock)):
        return result

    # Check 6: Skill subset consistency (manifest vs lockfile)
    if _run(_check_skill_subset_consistency(manifest, lock)):
        return result

    # Check 7: Config consistency (MCP)
    if _run(_check_config_consistency(manifest, lock)):
        return result

    # Check 8: Content integrity
    if _run(_check_content_integrity(project_root, lock)):
        return result

    # Check 9: Includes consent (advisory; never hard-fails)
    _run(_check_includes_consent(manifest, lock))

    return result
