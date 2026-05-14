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
from typing import TYPE_CHECKING, List, Optional, Sequence  # noqa: F401, UP035

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


def _check_config_consistency(
    manifest: APMPackage,
    lock: LockFile,
) -> CheckResult:
    """Verify MCP server configs match lockfile baseline."""
    from ..drift import detect_config_drift
    from ..integration.mcp_integrator import MCPIntegrator

    mcp_deps = manifest.get_mcp_dependencies()
    current_configs = MCPIntegrator.get_server_configs(mcp_deps)
    stored_configs = lock.mcp_configs or {}

    # No MCP deps at all -- nothing to check
    if not current_configs and not stored_configs:
        return CheckResult(
            name="config-consistency",
            passed=True,
            message="No MCP configs to check",
        )

    details: list[str] = []

    # Detect drift on servers that exist in both sets
    drifted = detect_config_drift(current_configs, stored_configs)
    for name in sorted(drifted):
        details.append(f"{name}: config differs from lockfile baseline")

    # Servers in lockfile but not in manifest (orphaned MCP)
    for name in sorted(stored_configs):
        if name not in current_configs:
            details.append(f"{name}: in lockfile but not in manifest")

    # Servers in manifest but not in lockfile (new, not installed)
    for name in sorted(current_configs):
        if name not in stored_configs:
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
      * SHA-256 drift between the on-disk content and the hash recorded
        in ``deployed_file_hashes`` at install time.

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

    # Per-file hash verification across all dependencies (the synthesized
    # self-entry is included in ``lock.dependencies`` so local content is
    # covered through the same iteration).
    hash_mismatches: list[tuple] = []  # (dep_key, rel_path, expected, actual)
    # Local import: matches the scoping pattern used in
    # _check_deployed_files_present (line 131); avoids cycles.
    from ..integration.base_integrator import BaseIntegrator as _BaseIntegrator

    for dep_key, dep in lock.dependencies.items():
        if not dep.deployed_file_hashes:
            continue
        for rel_path, expected_hash in dep.deployed_file_hashes.items():
            # Path safety: silently skip any rel_path that escapes
            # project_root or targets a non-allowlisted prefix.  Mirrors
            # the guard in _check_deployed_files_present so a forged
            # lockfile cannot induce reads outside managed locations.
            safe_rel = rel_path.rstrip("/")
            if not _BaseIntegrator.validate_deploy_path(safe_rel, project_root):
                continue
            file_path = project_root / safe_rel
            if not file_path.exists():
                continue  # _check_deployed_files_present owns this signal
            if file_path.is_symlink():
                continue
            if not file_path.is_file():
                continue
            actual_hash = compute_file_hash(file_path)
            if actual_hash != expected_hash:
                hash_mismatches.append((dep_key, rel_path, expected_hash, actual_hash))

    if not critical_files and not hash_mismatches:
        return CheckResult(
            name="content-integrity",
            passed=True,
            message="No critical hidden Unicode or hash drift detected",
        )

    details: list[str] = []
    for rel_path in critical_files:
        details.append(f"unicode: {rel_path}")
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
    from ..models.apm_package import APMPackage, clear_apm_yml_cache

    result = CIAuditResult()
    apm_yml_path = project_root / "apm.yml"

    # Parse manifest ONCE -- this function owns parse-error handling.
    manifest = None
    if apm_yml_path.exists():
        import yaml

        try:
            clear_apm_yml_cache()
            manifest = APMPackage.from_apm_yml(apm_yml_path)
        except (ValueError, yaml.YAMLError, OSError) as exc:
            result.checks.append(
                CheckResult(
                    name="manifest-parse",
                    passed=False,
                    message="Cannot parse apm.yml: %s -- fix the YAML syntax error in apm.yml and re-run."  # noqa: UP031
                    % exc,
                )
            )
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

    # Check 2: Ref consistency
    if _run(_check_ref_consistency(manifest, lock)):
        return result

    # Check 3: Deployed files present
    if _run(_check_deployed_files_present(project_root, lock)):
        return result

    # Check 4: No orphaned packages
    if _run(_check_no_orphans(manifest, lock)):
        return result

    # Check 4.5: Skill subset consistency (manifest vs lockfile)
    if _run(_check_skill_subset_consistency(manifest, lock)):
        return result

    # Check 5: Config consistency (MCP)
    if _run(_check_config_consistency(manifest, lock)):
        return result

    # Check 6: Content integrity
    if _run(_check_content_integrity(project_root, lock)):
        return result

    # Check 7: Includes consent (advisory; never hard-fails)
    _run(_check_includes_consent(manifest, lock))

    return result
