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
from typing import TYPE_CHECKING

from ..deps.lockfile import _SELF_KEY, LEGACY_LOCKFILE_NAME, LOCKFILE_NAME
from ._drift_check import DRIFT_SKIP_PREFIX, _check_drift  # noqa: F401
from ._integrity_check import _check_content_integrity
from .models import CheckResult, CIAuditResult

if TYPE_CHECKING:
    from ..deps.lockfile import LockFile

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

    has_deps = manifest.has_apm_dependencies() or bool(manifest.get_mcp_dependencies())
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
    """Verify every dependency's manifest ref matches lockfile resolved_ref."""
    from ..drift import detect_ref_change

    mismatches: list[str] = []
    for dep_ref in manifest.get_apm_dependencies():
        key = dep_ref.get_unique_key()
        locked_dep = lock.get_dependency(key)
        if locked_dep is None:
            mismatches.append(f"{key}: not found in lockfile")
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
    return CheckResult(
        name="ref-consistency",
        passed=False,
        message=f"{len(mismatches)} ref mismatch(es) -- run 'apm install' to update lockfile",
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


def _check_no_orphans(
    manifest: APMPackage,
    lock: LockFile,
) -> CheckResult:
    """Verify no packages in lockfile are absent from manifest."""
    manifest_keys = {dep.get_unique_key() for dep in manifest.get_apm_dependencies()}
    orphaned = [
        dep_key
        for dep_key in lock.dependencies
        if dep_key not in manifest_keys and dep_key != _SELF_KEY
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
    for dep_ref in manifest.get_apm_dependencies():
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


from ._mcp_checks import _check_config_consistency, _check_includes_consent  # noqa: E402

# -- Aggregate runner ----------------------------------------------


def _load_lockfile_and_check(
    project_root: Path,
    apm_yml_path: Path,
    result: CIAuditResult,
    ci_mode: bool,
):
    """Load lockfile if exists or handle missing manifest case."""
    from ..deps.lockfile import LockFile, get_lockfile_path

    lockfile_path = get_lockfile_path(project_root)

    # Check for manifest-missing artifacts before running remaining checks
    if not apm_yml_path.exists() or not lockfile_path.exists():
        _check_missing_manifest(project_root, apm_yml_path, result, ci_mode)
        return None

    lock = LockFile.read(lockfile_path)
    return lock


def _run_baseline_check_suite(
    project_root: Path,
    manifest,
    lock,
    result: CIAuditResult,
    fail_fast: bool,
) -> None:
    """Run the full suite of baseline checks."""

    def _run(check: CheckResult) -> bool:
        """Append check and return True if fail-fast should stop."""
        result.checks.append(check)
        return fail_fast and not check.passed

    # Run remaining checks with fail-fast support
    if _run(_check_ref_consistency(manifest, lock)):
        return
    if _run(_check_deployed_files_present(project_root, lock)):
        return
    if _run(_check_no_orphans(manifest, lock)):
        return
    if _run(_check_skill_subset_consistency(manifest, lock)):
        return
    if _run(_check_config_consistency(manifest, lock)):
        return
    if _run(_check_content_integrity(project_root, lock)):
        return
    _run(_check_includes_consent(manifest, lock))


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
    result = CIAuditResult()
    apm_yml_path = project_root / "apm.yml"

    # Parse manifest ONCE -- this function owns parse-error handling.
    manifest = _parse_manifest(apm_yml_path, result)
    if manifest is None and result.checks:
        return result

    # Check 1: Lockfile exists (manifest already parsed, pass it in)
    result.checks.append(_check_lockfile_exists(project_root, manifest))
    if not result.checks[0].passed:
        return result

    # Load lockfile or handle missing manifest
    lock = _load_lockfile_and_check(project_root, apm_yml_path, result, ci_mode)
    if lock is None:
        return result

    # Run remaining checks
    _run_baseline_check_suite(project_root, manifest, lock, result, fail_fast)

    return result


def _parse_manifest(apm_yml_path: Path, result: CIAuditResult) -> APMPackage | None:
    """Parse manifest and handle errors; returns None on failure."""
    from ..models.apm_package import APMPackage, clear_apm_yml_cache

    if not apm_yml_path.exists():
        return None

    import yaml

    try:
        clear_apm_yml_cache()
        return APMPackage.from_apm_yml(apm_yml_path)
    except (ValueError, yaml.YAMLError, OSError) as exc:
        result.checks.append(
            CheckResult(
                name="manifest-parse",
                passed=False,
                message=f"Cannot parse apm.yml: {exc} -- fix the YAML syntax error in apm.yml and re-run.",
            )
        )
        return None


def _check_missing_manifest(
    project_root: Path,
    apm_yml_path: Path,
    result: CIAuditResult,
    ci_mode: bool,
) -> None:
    """Check for APM artifacts without manifest."""
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
