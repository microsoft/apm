"""Policy checks for organisational governance enforcement.

These checks run WITH a policy file and validate that the project's manifest,
lockfile, and on-disk state comply with the organisation's declared policies.
They are always run in addition to the baseline checks in ``ci_checks``.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .models import CIAuditResult, CheckResult


# -- Helpers -------------------------------------------------------


def _load_raw_apm_yml(project_root: Path) -> Optional[dict]:
    """Load raw apm.yml as a dict for policy checks that inspect raw fields."""
    import yaml

    apm_yml_path = project_root / "apm.yml"
    if not apm_yml_path.exists():
        return None
    try:
        with open(apm_yml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# -- Individual policy checks --------------------------------------


def _check_dependency_allowlist(
    deps: List["DependencyReference"],
    policy: "DependencyPolicy",
) -> CheckResult:
    """Check 1: every dependency matches policy allow list."""
    from .matcher import check_dependency_allowed

    if policy.allow is None:
        return CheckResult(
            name="dependency-allowlist",
            passed=True,
            message="No dependency allow list configured",
        )

    violations: List[str] = []
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
    deps: List["DependencyReference"],
    policy: "DependencyPolicy",
) -> CheckResult:
    """Check 2: no dependency matches policy deny list."""
    from .matcher import check_dependency_allowed

    if not policy.deny:
        return CheckResult(
            name="dependency-denylist",
            passed=True,
            message="No dependency deny list configured",
        )

    violations: List[str] = []
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
    deps: List["DependencyReference"],
    policy: "DependencyPolicy",
) -> CheckResult:
    """Check 3: every required package is in manifest deps."""
    if not policy.require:
        return CheckResult(
            name="required-packages",
            passed=True,
            message="No required packages configured",
        )

    dep_names = {
        dep.get_canonical_dependency_string().split("#")[0] for dep in deps
    }
    missing: List[str] = []
    for req in policy.require:
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
    deps: List["DependencyReference"],
    lock: Optional["LockFile"],
    policy: "DependencyPolicy",
) -> CheckResult:
    """Check 4: required packages appear in lockfile with deployed files."""
    if not policy.require or lock is None:
        return CheckResult(
            name="required-packages-deployed",
            passed=True,
            message="No required packages to verify deployment",
        )

    dep_names = {
        dep.get_canonical_dependency_string().split("#")[0] for dep in deps
    }
    lock_by_name = {
        locked.get_unique_key(): locked
        for _key, locked in lock.dependencies.items()
    }
    not_deployed: List[str] = []
    for req in policy.require:
        pkg_name = req.split("#")[0]
        if pkg_name not in dep_names:
            continue  # not in manifest -- check 3 handles this

        # Find in lockfile by exact key match
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
        message=f"{len(not_deployed)} required package(s) not deployed",
        details=not_deployed,
    )


def _check_required_package_version(
    deps: List["DependencyReference"],
    lock: Optional["LockFile"],
    policy: "DependencyPolicy",
) -> CheckResult:
    """Check 5: required packages with version pins match per resolution strategy."""
    pinned = [(r, r.split("#", 1)) for r in policy.require if "#" in r]
    if not pinned or lock is None:
        return CheckResult(
            name="required-package-version",
            passed=True,
            message="No version-pinned required packages",
        )

    resolution = policy.require_resolution
    violations: List[str] = []
    warnings: List[str] = []

    lock_by_name = {
        locked.get_unique_key(): locked
        for _key, locked in lock.dependencies.items()
    }

    for _req, parts in pinned:
        pkg_name, expected_ref = parts[0], parts[1]

        locked = lock_by_name.get(pkg_name)
        if locked is not None:
            actual_ref = locked.resolved_ref or ""
            if actual_ref != expected_ref:
                detail = (
                    f"{pkg_name}: expected ref '{expected_ref}', "
                    f"got '{actual_ref}'"
                )
                if resolution == "block":
                    violations.append(detail)
                elif resolution == "policy-wins":
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
    lock: Optional["LockFile"],
    policy: "DependencyPolicy",
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

    violations: List[str] = []
    for key, dep in lock.dependencies.items():
        if dep.depth > policy.max_depth:
            violations.append(
                f"{key}: depth {dep.depth} exceeds limit {policy.max_depth}"
            )

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


def _check_mcp_allowlist(
    mcp_deps: List,
    policy: "McpPolicy",
) -> CheckResult:
    """Check 7: MCP server names match allow list."""
    from .matcher import check_mcp_allowed

    if policy.allow is None:
        return CheckResult(
            name="mcp-allowlist",
            passed=True,
            message="No MCP allow list configured",
        )

    violations: List[str] = []
    for mcp in mcp_deps:
        allowed, reason = check_mcp_allowed(mcp.name, policy)
        if not allowed and "not in allowed" in reason:
            violations.append(f"{mcp.name}: {reason}")

    if not violations:
        return CheckResult(
            name="mcp-allowlist",
            passed=True,
            message="All MCP servers match allow list",
        )
    return CheckResult(
        name="mcp-allowlist",
        passed=False,
        message=f"{len(violations)} MCP server(s) not in allow list",
        details=violations,
    )


def _check_mcp_denylist(
    mcp_deps: List,
    policy: "McpPolicy",
) -> CheckResult:
    """Check 8: no MCP server matches deny list."""
    from .matcher import check_mcp_allowed

    if not policy.deny:
        return CheckResult(
            name="mcp-denylist",
            passed=True,
            message="No MCP deny list configured",
        )

    violations: List[str] = []
    for mcp in mcp_deps:
        allowed, reason = check_mcp_allowed(mcp.name, policy)
        if not allowed and "denied by pattern" in reason:
            violations.append(f"{mcp.name}: {reason}")

    if not violations:
        return CheckResult(
            name="mcp-denylist",
            passed=True,
            message="No MCP servers match deny list",
        )
    return CheckResult(
        name="mcp-denylist",
        passed=False,
        message=f"{len(violations)} MCP server(s) match deny list",
        details=violations,
    )


def _check_mcp_transport(
    mcp_deps: List,
    policy: "McpPolicy",
) -> CheckResult:
    """Check 9: MCP transport values match policy allow list."""
    allowed_transports = policy.transport.allow
    if allowed_transports is None:
        return CheckResult(
            name="mcp-transport",
            passed=True,
            message="No MCP transport restrictions configured",
        )

    violations: List[str] = []
    for mcp in mcp_deps:
        if mcp.transport and mcp.transport not in allowed_transports:
            violations.append(
                f"{mcp.name}: transport '{mcp.transport}' not in allowed {allowed_transports}"
            )

    if not violations:
        return CheckResult(
            name="mcp-transport",
            passed=True,
            message="All MCP transports comply with policy",
        )
    return CheckResult(
        name="mcp-transport",
        passed=False,
        message=f"{len(violations)} MCP transport violation(s)",
        details=violations,
    )


def _check_mcp_self_defined(
    mcp_deps: List,
    policy: "McpPolicy",
) -> CheckResult:
    """Check 10: self-defined MCP servers comply with policy."""
    self_defined_policy = policy.self_defined
    if self_defined_policy == "allow":
        return CheckResult(
            name="mcp-self-defined",
            passed=True,
            message="Self-defined MCP servers allowed",
        )

    self_defined = [m for m in mcp_deps if m.registry is False]
    if not self_defined:
        return CheckResult(
            name="mcp-self-defined",
            passed=True,
            message="No self-defined MCP servers found",
        )

    details = [f"{m.name}: self-defined server" for m in self_defined]
    if self_defined_policy == "deny":
        return CheckResult(
            name="mcp-self-defined",
            passed=False,
            message=f"{len(self_defined)} self-defined MCP server(s) denied by policy",
            details=details,
        )
    # warn -- pass but with details
    return CheckResult(
        name="mcp-self-defined",
        passed=True,
        message=f"{len(self_defined)} self-defined MCP server(s) (warn)",
        details=details,
    )


def _check_compilation_target(
    raw_yml: Optional[dict],
    policy: "CompilationPolicy",
) -> CheckResult:
    """Check 11: compilation target matches policy."""
    enforce = policy.target.enforce
    allow = policy.target.allow

    if not enforce and allow is None:
        return CheckResult(
            name="compilation-target",
            passed=True,
            message="No compilation target restrictions configured",
        )

    target = (raw_yml or {}).get("target")
    if not target:
        return CheckResult(
            name="compilation-target",
            passed=True,
            message="No compilation target set in manifest",
        )

    # Normalize target to a list for uniform checking
    target_list = target if isinstance(target, list) else [target]

    if enforce:
        if enforce not in target_list:
            return CheckResult(
                name="compilation-target",
                passed=False,
                message=f"Enforced target '{enforce}' not present in {target_list}",
                details=[f"target: {target}, enforced: {enforce}"],
            )
    elif allow is not None:
        allow_set = set(allow) if isinstance(allow, list) else {allow}
        disallowed = [t for t in target_list if t not in allow_set]
        if disallowed:
            return CheckResult(
                name="compilation-target",
                passed=False,
                message=f"Target(s) {disallowed} not in allowed list {sorted(allow_set)}",
                details=[f"target: {target}, allowed: {sorted(allow_set)}"],
            )

    return CheckResult(
        name="compilation-target",
        passed=True,
        message="Compilation target compliant",
    )


def _check_compilation_strategy(
    raw_yml: Optional[dict],
    policy: "CompilationPolicy",
) -> CheckResult:
    """Check 12: compilation strategy matches policy."""
    enforce = policy.strategy.enforce
    if not enforce:
        return CheckResult(
            name="compilation-strategy",
            passed=True,
            message="No compilation strategy enforced",
        )

    compilation = (raw_yml or {}).get("compilation", {})
    strategy = compilation.get("strategy") if isinstance(compilation, dict) else None
    if not strategy:
        return CheckResult(
            name="compilation-strategy",
            passed=True,
            message="No compilation strategy set in manifest",
        )

    if strategy != enforce:
        return CheckResult(
            name="compilation-strategy",
            passed=False,
            message=f"Strategy '{strategy}' does not match enforced '{enforce}'",
            details=[f"strategy: {strategy}, enforced: {enforce}"],
        )
    return CheckResult(
        name="compilation-strategy",
        passed=True,
        message="Compilation strategy compliant",
    )


def _check_source_attribution(
    raw_yml: Optional[dict],
    policy: "CompilationPolicy",
) -> CheckResult:
    """Check 13: source attribution enabled if policy requires."""
    if not policy.source_attribution:
        return CheckResult(
            name="source-attribution",
            passed=True,
            message="Source attribution not required by policy",
        )

    compilation = (raw_yml or {}).get("compilation", {})
    attribution = (
        compilation.get("source_attribution")
        if isinstance(compilation, dict)
        else None
    )
    if attribution is True:
        return CheckResult(
            name="source-attribution",
            passed=True,
            message="Source attribution enabled",
        )
    return CheckResult(
        name="source-attribution",
        passed=False,
        message="Source attribution required by policy but not enabled in manifest",
        details=["Set compilation.source_attribution: true in apm.yml"],
    )


def _check_required_manifest_fields(
    raw_yml: Optional[dict],
    policy: "ManifestPolicy",
) -> CheckResult:
    """Check 14: all required fields are present with non-empty values."""
    if not policy.required_fields:
        return CheckResult(
            name="required-manifest-fields",
            passed=True,
            message="No required manifest fields configured",
        )

    data = raw_yml or {}
    missing: List[str] = []
    for field_name in policy.required_fields:
        value = data.get(field_name)
        if not value:  # None, empty string, missing
            missing.append(field_name)

    if not missing:
        return CheckResult(
            name="required-manifest-fields",
            passed=True,
            message="All required manifest fields present",
        )
    return CheckResult(
        name="required-manifest-fields",
        passed=False,
        message=f"{len(missing)} required manifest field(s) missing",
        details=missing,
    )


def _check_scripts_policy(
    raw_yml: Optional[dict],
    policy: "ManifestPolicy",
) -> CheckResult:
    """Check 15: scripts section absent if policy denies it."""
    if policy.scripts != "deny":
        return CheckResult(
            name="scripts-policy",
            passed=True,
            message="Scripts allowed by policy",
        )

    scripts = (raw_yml or {}).get("scripts")
    if scripts:
        return CheckResult(
            name="scripts-policy",
            passed=False,
            message="Scripts section present but denied by policy",
            details=list(scripts.keys()) if isinstance(scripts, dict) else ["scripts"],
        )
    return CheckResult(
        name="scripts-policy",
        passed=True,
        message="No scripts section (compliant with deny policy)",
    )


_DEFAULT_GOVERNANCE_DIRS = [
    ".github/agents",
    ".github/instructions",
    ".github/hooks",
    ".cursor/rules",
    ".claude",
    ".opencode",
]


_MAX_UNMANAGED_SCAN_FILES = 10_000


def _check_unmanaged_files(
    project_root: Path,
    lock: Optional["LockFile"],
    policy: "UnmanagedFilesPolicy",
) -> CheckResult:
    """Check 16: no untracked files in governance directories."""
    if policy.action == "ignore":
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message="Unmanaged files check disabled (action: ignore)",
        )

    dirs = policy.directories if policy.directories else _DEFAULT_GOVERNANCE_DIRS

    # Build set of deployed files AND directory prefixes from lockfile
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

    unmanaged: List[str] = []
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

    if policy.action == "warn":
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message=f"{len(unmanaged)} unmanaged file(s) found (warn)",
            details=unmanaged,
        )

    # action == "deny"
    return CheckResult(
        name="unmanaged-files",
        passed=False,
        message=f"{len(unmanaged)} unmanaged file(s) in governance directories",
        details=unmanaged,
    )


# -- Aggregate runner ----------------------------------------------


def run_policy_checks(
    project_root: Path,
    policy: "ApmPolicy",
    *,
    fail_fast: bool = True,
) -> CIAuditResult:
    """Run policy checks against a project.

    These checks are ADDED to baseline checks -- caller runs both.
    When *fail_fast* is ``True`` (default), stops after the first
    failing check.
    Returns :class:`CIAuditResult` with individual check results.
    """
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ..models.apm_package import APMPackage, clear_apm_yml_cache

    result = CIAuditResult()

    # Load manifest
    apm_yml_path = project_root / "apm.yml"
    if not apm_yml_path.exists():
        return result

    try:
        clear_apm_yml_cache()
        manifest = APMPackage.from_apm_yml(apm_yml_path)
    except (ValueError, FileNotFoundError):
        return result

    # Load lockfile (optional -- some checks work without it)
    lockfile_path = get_lockfile_path(project_root)
    lock = LockFile.read(lockfile_path) if lockfile_path.exists() else None

    # Load raw YAML for field-level checks
    raw_yml = _load_raw_apm_yml(project_root)

    # Get dependencies
    apm_deps = manifest.get_apm_dependencies()
    mcp_deps = manifest.get_mcp_dependencies()

    def _run(check: CheckResult) -> bool:
        """Append check and return True if fail-fast should stop."""
        result.checks.append(check)
        return fail_fast and not check.passed

    # Dependency checks (1-6)
    if _run(_check_dependency_allowlist(apm_deps, policy.dependencies)):
        return result
    if _run(_check_dependency_denylist(apm_deps, policy.dependencies)):
        return result
    if _run(_check_required_packages(apm_deps, policy.dependencies)):
        return result
    if _run(
        _check_required_packages_deployed(apm_deps, lock, policy.dependencies)
    ):
        return result
    if _run(
        _check_required_package_version(apm_deps, lock, policy.dependencies)
    ):
        return result
    if _run(_check_transitive_depth(lock, policy.dependencies)):
        return result

    # MCP checks (7-10)
    if _run(_check_mcp_allowlist(mcp_deps, policy.mcp)):
        return result
    if _run(_check_mcp_denylist(mcp_deps, policy.mcp)):
        return result
    if _run(_check_mcp_transport(mcp_deps, policy.mcp)):
        return result
    if _run(_check_mcp_self_defined(mcp_deps, policy.mcp)):
        return result

    # Compilation checks (11-13)
    if _run(_check_compilation_target(raw_yml, policy.compilation)):
        return result
    if _run(_check_compilation_strategy(raw_yml, policy.compilation)):
        return result
    if _run(_check_source_attribution(raw_yml, policy.compilation)):
        return result

    # Manifest checks (14-15)
    if _run(_check_required_manifest_fields(raw_yml, policy.manifest)):
        return result
    if _run(_check_scripts_policy(raw_yml, policy.manifest)):
        return result

    # Unmanaged files check (16)
    _run(
        _check_unmanaged_files(project_root, lock, policy.unmanaged_files)
    )

    return result
