"""Policy checks for organisational governance enforcement.

These checks run WITH a policy file and validate that the project's manifest,
lockfile, and on-disk state comply with the organisation's declared policies.
They are always run in addition to the baseline checks in ``ci_checks``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ._policy_checks_mcp import (
    _check_compilation_strategy as _check_compilation_strategy,
)
from ._policy_checks_mcp import (
    _check_compilation_target as _check_compilation_target,
)
from ._policy_checks_mcp import (
    _check_includes_explicit as _check_includes_explicit,
)
from ._policy_checks_mcp import (
    _check_mcp_allowlist as _check_mcp_allowlist,
)
from ._policy_checks_mcp import (
    _check_mcp_denylist as _check_mcp_denylist,
)
from ._policy_checks_mcp import (
    _check_mcp_self_defined as _check_mcp_self_defined,
)
from ._policy_checks_mcp import (
    _check_mcp_transport as _check_mcp_transport,
)
from ._policy_checks_mcp import (
    _check_required_manifest_fields as _check_required_manifest_fields,
)
from ._policy_checks_mcp import (
    _check_scripts_policy as _check_scripts_policy,
)
from ._policy_checks_mcp import (
    _check_source_attribution as _check_source_attribution,
)
from ._policy_checks_mcp import (
    _load_raw_apm_yml as _load_raw_apm_yml,
)
from .models import CheckResult, CIAuditResult

if TYPE_CHECKING:
    from ..deps.lockfile import LockFile
    from .schema import (
        ApmPolicy,
        DependencyPolicy,
        DependencyReference,
        RegistrySourcePolicy,
        UnmanagedFilesPolicy,
    )

_logger = logging.getLogger(__name__)

# -- Sentinel for "manifest_includes not provided" in run_dependency_policy_checks --
_INCLUDES_NOT_PROVIDED = object()

_DEFAULT_GOVERNANCE_DIRS = [
    ".github/agents",
    ".github/instructions",
    ".github/hooks",
    ".cursor/rules",
    ".claude",
    ".opencode",
    ".kiro",
]

_MAX_UNMANAGED_SCAN_FILES = 10_000


# -- Individual policy checks (dependency cluster) -------------------------


def _check_dependency_allowlist(
    deps: list[DependencyReference],
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 1: every dependency matches policy allow list."""
    from .matcher import check_dependency_allowed

    if policy.allow is None:
        return CheckResult(
            name="dependency-allowlist",
            passed=True,
            message="No dependency allow list configured",
        )

    violations: list[str] = []
    for dep in deps:
        ref = dep.get_canonical_dependency_string()
        allowed, reason = check_dependency_allowed(ref, policy)
        if not allowed and "not in allowed" in reason:
            violations.append(f"{ref}: {reason}")

    if not violations:
        return CheckResult(
            name="dependency-allowlist",
            passed=True,
            message="All dependencies match allow list",
        )
    return CheckResult(
        name="dependency-allowlist",
        passed=False,
        message=f"{len(violations)} dependency(ies) not in allow list",
        details=violations,
    )


def _check_dependency_denylist(
    deps: list[DependencyReference],
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 2: no dependency matches policy deny list."""
    from .matcher import check_dependency_allowed

    if not policy.effective_deny:
        return CheckResult(
            name="dependency-denylist",
            passed=True,
            message="No dependency deny list configured",
        )

    violations: list[str] = []
    for dep in deps:
        ref = dep.get_canonical_dependency_string()
        allowed, reason = check_dependency_allowed(ref, policy)
        if not allowed and "denied by pattern" in reason:
            violations.append(f"{ref}: {reason}")

    if not violations:
        return CheckResult(
            name="dependency-denylist",
            passed=True,
            message="No dependencies match deny list",
        )
    return CheckResult(
        name="dependency-denylist",
        passed=False,
        message=f"{len(violations)} dependency(ies) match deny list",
        details=violations,
    )


def _check_required_packages(
    deps: list[DependencyReference],
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 3: every required package is in manifest deps."""
    if not policy.effective_require:
        return CheckResult(
            name="required-packages",
            passed=True,
            message="No required packages configured",
        )

    dep_names = {dep.get_canonical_dependency_string().split("#")[0] for dep in deps}
    missing: list[str] = []
    for req in policy.effective_require:
        pkg_name = req.split("#")[0]
        if pkg_name not in dep_names:
            missing.append(pkg_name)

    if not missing:
        return CheckResult(
            name="required-packages",
            passed=True,
            message="All required packages present in manifest",
        )
    return CheckResult(
        name="required-packages",
        passed=False,
        message=f"{len(missing)} required package(s) missing from manifest",
        details=missing,
    )


def _check_required_packages_deployed(
    deps: list[DependencyReference],
    lock: LockFile | None,
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 4: required packages appear in lockfile with deployed files."""
    if not policy.effective_require or lock is None:
        return CheckResult(
            name="required-packages-deployed",
            passed=True,
            message="No required packages to verify deployment",
        )

    dep_names = {dep.get_canonical_dependency_string().split("#")[0] for dep in deps}
    lock_by_name = {locked.get_unique_key(): locked for _key, locked in lock.dependencies.items()}
    not_deployed: list[str] = []
    for req in policy.effective_require:
        pkg_name = req.split("#")[0]
        if pkg_name not in dep_names:
            continue  # not in manifest -- check 3 handles this

        locked = lock_by_name.get(pkg_name)
        if not locked or not locked.deployed_files:
            not_deployed.append(pkg_name)

    if not not_deployed:
        return CheckResult(
            name="required-packages-deployed",
            passed=True,
            message="All required packages deployed",
        )
    return CheckResult(
        name="required-packages-deployed",
        passed=False,
        message=(
            f"{len(not_deployed)} required package(s) not deployed. "
            "Hint: run `apm install --no-policy` to repair the lockfile, "
            "then reinstall normally."
        ),
        details=not_deployed,
    )


def _check_required_package_version(
    deps: list[DependencyReference],
    lock: LockFile | None,
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 5: required packages with version pins match per resolution strategy."""
    pinned = [(r, r.split("#", 1)) for r in policy.effective_require if "#" in r]
    if not pinned or lock is None:
        return CheckResult(
            name="required-package-version",
            passed=True,
            message="No version-pinned required packages",
        )

    resolution = policy.require_resolution
    violations: list[str] = []
    warnings: list[str] = []

    lock_by_name = {locked.get_unique_key(): locked for _key, locked in lock.dependencies.items()}

    for _req, parts in pinned:
        pkg_name, expected_ref = parts[0], parts[1]

        locked = lock_by_name.get(pkg_name)
        if locked is not None:
            actual_ref = locked.resolved_ref or ""
            if actual_ref != expected_ref:
                detail = f"{pkg_name}: expected ref '{expected_ref}', got '{actual_ref}'"
                if resolution == "block" or resolution == "policy-wins":  # noqa: PLR1714
                    violations.append(detail)
                else:  # project-wins
                    warnings.append(detail)

    if not violations:
        return CheckResult(
            name="required-package-version",
            passed=True,
            message="Required package versions match"
            + (f" (warnings: {len(warnings)})" if warnings else ""),
            details=warnings,
        )
    return CheckResult(
        name="required-package-version",
        passed=False,
        message=f"{len(violations)} version mismatch(es)",
        details=violations,
    )


def _check_transitive_depth(
    lock: LockFile | None,
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 6: no lockfile dep exceeds max_depth."""
    if lock is None or policy.max_depth >= 50:
        return CheckResult(
            name="transitive-depth",
            passed=True,
            message="No transitive depth limit configured"
            if policy.max_depth >= 50
            else "No lockfile to check",
        )

    violations: list[str] = []
    for key, dep in lock.dependencies.items():
        if dep.depth > policy.max_depth:
            violations.append(f"{key}: depth {dep.depth} exceeds limit {policy.max_depth}")

    if not violations:
        return CheckResult(
            name="transitive-depth",
            passed=True,
            message=f"All dependencies within depth limit ({policy.max_depth})",
        )
    return CheckResult(
        name="transitive-depth",
        passed=False,
        message=f"{len(violations)} dependency(ies) exceed max depth {policy.max_depth}",
        details=violations,
    )


def _check_registry_source(
    deps: list[DependencyReference],
    policy: RegistrySourcePolicy,
    registries_map: dict[str, str] | None,
) -> CheckResult:
    """Check registry source policy (require / allow_non_registry).

    Fail-closed when a required registry name has no URL configured in
    *registries_map* -- that means the registry source is unreachable by
    definition and the install must not proceed.
    """
    check_name = "registry-source"
    no_op = not policy.require and policy.allow_non_registry
    if no_op:
        return CheckResult(name=check_name, passed=True, message="No registry source policy")

    violations: list[str] = []

    # Fail-closed: required registry names must be configured.
    for req_name in policy.require:
        if not registries_map or req_name not in registries_map:
            violations.append(
                f"required registry '{req_name}' is not configured -- "
                "add it to the 'registries:' block or via 'apm config set registry."
                f"{req_name}.url <url>'"
            )

    for dep in deps:
        key = dep.get_canonical_dependency_string()
        is_registry = getattr(dep, "source", None) == "registry"
        registry_name = getattr(dep, "registry_name", None)

        if not policy.allow_non_registry and not is_registry:
            violations.append(
                f"{key}: non-registry source not permitted (policy requires registry sources only)"
            )
            continue

        if policy.require and is_registry and registry_name not in policy.require:
            violations.append(
                f"{key}: sourced from registry '{registry_name}' "
                f"but policy requires one of {sorted(policy.require)}"
            )

    if violations:
        return CheckResult(
            name=check_name,
            passed=False,
            message=f"{len(violations)} registry source violation(s)",
            details=violations,
        )
    return CheckResult(
        name=check_name,
        passed=True,
        message="All dependencies satisfy registry source policy",
    )


def _check_pinned_constraints(
    deps: list[DependencyReference],
    policy: DependencyPolicy,
    direct_dep_keys: set[str] | None = None,
) -> CheckResult:
    """Check: every direct dep declares a bounded constraint.

    Skipped (passes vacuously) when
    ``policy.require_pinned_constraint`` is ``False`` -- the default.

    Operates on the **declared** constraint (``dep.reference``), not
    the resolved one, so authors learn before the install completes
    that a moving ref slipped past review.

    When ``direct_dep_keys`` is provided, the check is restricted to
    direct dependencies only -- transitives are excluded, since the
    consumer cannot rewrite a constraint declared in a transitive
    package's own manifest.

    See ``_constraint_pinning.py`` for classification rules.
    """
    from ._constraint_pinning import classify_unbounded_reason, humanize_reason

    check_name = "dependency-pinned-constraint"
    if not policy.require_pinned_constraint:
        return CheckResult(
            name=check_name,
            passed=True,
            message="Pinned-constraint requirement disabled",
        )

    violations: list[str] = []
    for dep in deps:
        if direct_dep_keys is not None and dep.get_unique_key() not in direct_dep_keys:
            continue
        reason = classify_unbounded_reason(dep)
        if reason is None:
            continue
        key = dep.get_canonical_dependency_string()
        hint = humanize_reason(reason, dep)
        violations.append(f"{key}: {hint}")

    if not violations:
        return CheckResult(
            name=check_name,
            passed=True,
            message="All dependencies use pinned constraints",
        )

    return CheckResult(
        name=check_name,
        passed=False,
        message=(
            f"{len(violations)} dependency(ies) use unbounded constraints "
            "(hint: pin to a semver range, literal tag, or SHA)"
        ),
        details=violations,
    )


def _check_unmanaged_files(
    project_root: Path,
    lock: LockFile | None,
    policy: UnmanagedFilesPolicy,
) -> CheckResult:
    """Check 16: no untracked files in governance directories."""
    if policy.effective_action == "ignore":
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message="Unmanaged files check disabled (action: ignore)",
        )

    dirs = policy.directories if policy.directories else _DEFAULT_GOVERNANCE_DIRS

    deployed: set = set()
    deployed_dir_prefixes: list = []
    if lock:
        for _key, dep in lock.dependencies.items():
            for f in dep.deployed_files:
                cleaned = f.rstrip("/")
                deployed.add(cleaned)
                if f.endswith("/"):
                    deployed_dir_prefixes.append(cleaned + "/")

    dir_prefix_tuple = tuple(deployed_dir_prefixes)

    unmanaged: list[str] = []
    files_scanned = 0
    cap_hit = False
    for gov_dir in dirs:
        dir_path = project_root / gov_dir
        if not dir_path.exists() or not dir_path.is_dir():
            continue
        for file_path in dir_path.rglob("*"):
            if file_path.is_file():
                files_scanned += 1
                if files_scanned > _MAX_UNMANAGED_SCAN_FILES:
                    cap_hit = True
                    break
                rel = file_path.relative_to(project_root).as_posix()
                if rel not in deployed and not (
                    dir_prefix_tuple and rel.startswith(dir_prefix_tuple)
                ):
                    unmanaged.append(rel)
        if cap_hit:
            break

    if cap_hit:
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message=(
                f"Scan capped at {_MAX_UNMANAGED_SCAN_FILES:,} files "
                "-- skipping unmanaged-files check"
            ),
            details=[
                f"Governance directories contain > {_MAX_UNMANAGED_SCAN_FILES:,} files; "
                "consider adding exclude patterns in a future policy version"
            ],
        )

    if not unmanaged:
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message="No unmanaged files in governance directories",
        )

    if policy.effective_action == "warn":
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message=f"{len(unmanaged)} unmanaged file(s) found (warn)",
            details=unmanaged,
        )

    return CheckResult(
        name="unmanaged-files",
        passed=False,
        message=f"{len(unmanaged)} unmanaged file(s) in governance directories",
        details=unmanaged,
    )


# -- Aggregate runners ---------------------------------------------


def run_dependency_policy_checks(
    deps_to_install,
    *,
    lockfile=None,
    policy: ApmPolicy,
    mcp_deps=None,
    effective_target: str | None = None,
    fetch_outcome: str | None = None,
    fail_fast: bool = True,
    manifest_includes=_INCLUDES_NOT_PROVIDED,
    registries: dict[str, str] | None = None,
    direct_dep_keys: set[str] | None = None,
) -> CIAuditResult:
    """Evaluate :class:`ApmPolicy` against an already-resolved dependency set.

    Used by both ``apm audit --ci`` (after resolving from disk) and the
    install pipeline ``policy_gate`` phase.  Reuses the private ``_check_*``
    helpers -- no logic duplication.

    Parameters
    ----------
    deps_to_install:
        Iterable of ``DependencyReference`` (the resolved set, including
        transitives).  This is what ``InstallContext.deps_to_install``
        contains after the resolve phase.
    lockfile:
        An ``ApmLockfile`` / ``LockFile`` instance, or ``None``.  Needed
        for deployed-files and version-pin checks.
    policy:
        The effective :class:`ApmPolicy` to enforce.
    mcp_deps:
        Iterable of ``MCPDependency`` objects, or ``None``.  When the
        resolved set includes MCP entries they are checked against
        ``policy.mcp``.
    effective_target:
        The post-targets-phase compilation target string, or ``None``.
        When ``None`` target/compilation checks are **skipped**.
    fetch_outcome:
        Human-readable label for diagnostic context.  Currently
        informational only.
    fail_fast:
        Stop after the first failing check (default ``True``).
    manifest_includes:
        The parsed value of the manifest's ``includes:`` field
        (``None``, ``"auto"``, or a list of paths).  When omitted,
        the ``explicit-includes`` check is skipped.
    direct_dep_keys:
        Optional set of ``DependencyReference.get_unique_key()`` for
        the direct (manifest-declared) deps. When supplied, the
        ``require_pinned_constraint`` check only evaluates direct
        deps -- transitive entries are excluded. When ``None`` every
        dep in ``deps_to_install`` is evaluated.

    Returns
    -------
    CIAuditResult
        Contains individual :class:`CheckResult` entries.  The caller
        decides how to map ``enforcement`` level (block vs warn) onto
        these results.
    """
    result = CIAuditResult()
    deps_list = list(deps_to_install)
    mcp_list = list(mcp_deps) if mcp_deps is not None else []

    def _run(check: CheckResult) -> bool:
        """Append check and return True if fail-fast should stop."""
        result.checks.append(check)
        return fail_fast and not check.passed

    # -- Dependency checks (1-6) -----------------------------------
    if _run(_check_dependency_allowlist(deps_list, policy.dependencies)):
        return result
    if _run(_check_dependency_denylist(deps_list, policy.dependencies)):
        return result
    if _run(_check_required_packages(deps_list, policy.dependencies)):
        return result
    if _run(_check_required_packages_deployed(deps_list, lockfile, policy.dependencies)):
        return result
    if _run(_check_required_package_version(deps_list, lockfile, policy.dependencies)):
        return result
    if _run(_check_transitive_depth(lockfile, policy.dependencies)):
        return result

    # -- Registry source + pinned-constraint + MCP + tail checks -----
    # Collect all remaining checks into a single loop so the function
    # stays within the max-returns threshold.
    remaining_checks: list[CheckResult] = [
        _check_registry_source(deps_list, policy.registry_source, registries),
        _check_pinned_constraints(deps_list, policy.dependencies, direct_dep_keys),
    ]
    if mcp_deps is not None:
        remaining_checks += [
            _check_mcp_allowlist(mcp_list, policy.mcp),
            _check_mcp_denylist(mcp_list, policy.mcp),
            _check_mcp_transport(mcp_list, policy.mcp),
            _check_mcp_self_defined(mcp_list, policy.mcp),
        ]
    if effective_target is not None:
        synthetic_yml = {"target": effective_target}
        remaining_checks.append(_check_compilation_target(synthetic_yml, policy.compilation))
    if manifest_includes is not _INCLUDES_NOT_PROVIDED:
        remaining_checks.append(_check_includes_explicit(manifest_includes, policy.manifest))
    for check in remaining_checks:
        if _run(check):
            return result

    return result


def run_policy_checks(
    project_root: Path,
    policy: ApmPolicy,
    *,
    fail_fast: bool = True,
) -> CIAuditResult:
    """Run the full set of policy checks against a project on disk.

    Thin wrapper: loads manifest + lockfile from *project_root*, resolves
    deps, and delegates dependency/MCP checks to
    :func:`run_dependency_policy_checks`.  Then appends the disk-level
    checks (compilation, manifest, unmanaged files) that require the raw
    ``apm.yml``.

    These checks are ADDED to baseline checks -- caller runs both.
    When *fail_fast* is ``True`` (default), stops after the first
    failing check.
    Returns :class:`CIAuditResult` with individual check results.
    """
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ._shared import _parse_apm_yml_safe

    result = CIAuditResult()

    apm_yml_path = project_root / "apm.yml"
    if not apm_yml_path.exists():
        return result

    manifest = _parse_apm_yml_safe(apm_yml_path, result)
    if manifest is None:
        return result

    lockfile_path = get_lockfile_path(project_root)
    lock = LockFile.read(lockfile_path) if lockfile_path.exists() else None
    raw_yml = _load_raw_apm_yml(project_root)

    apm_deps = manifest.get_apm_dependencies()
    mcp_deps = manifest.get_mcp_dependencies()

    dep_result = run_dependency_policy_checks(
        apm_deps,
        lockfile=lock,
        policy=policy,
        mcp_deps=mcp_deps,
        fail_fast=fail_fast,
        manifest_includes=manifest.includes,
        registries=getattr(manifest, "registries", None),
    )
    result.checks.extend(dep_result.checks)

    if fail_fast and not dep_result.passed:
        return result

    def _run(check: CheckResult) -> bool:
        result.checks.append(check)
        return fail_fast and not check.passed

    # Disk-level checks: compilation (11-13), manifest (14-15), unmanaged (16).
    # Eager evaluation is safe -- these check functions read dict/policy only,
    # no side effects except _check_unmanaged_files which must stay last.
    for check in [
        _check_compilation_target(raw_yml, policy.compilation),
        _check_compilation_strategy(raw_yml, policy.compilation),
        _check_source_attribution(raw_yml, policy.compilation),
        _check_required_manifest_fields(raw_yml, policy.manifest),
        _check_scripts_policy(raw_yml, policy.manifest),
    ]:
        if _run(check):
            return result

    _run(_check_unmanaged_files(project_root, lock, policy.unmanaged_files))
    return result
