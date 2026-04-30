"""Policy checks for organisational governance enforcement.

These checks run WITH a policy file and validate that the project's manifest,
lockfile, and on-disk state comply with the organisation's declared policies.
They are always run in addition to the baseline checks in ``ci_checks``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional  # noqa: F401, UP035

from .models import CheckResult, CIAuditResult

_logger = logging.getLogger(__name__)


# -- Helpers -------------------------------------------------------


def _load_raw_apm_yml(project_root: Path) -> dict | None:
    """Load raw apm.yml as a dict for policy checks that inspect raw fields.

    This helper is called **after** :pymethod:`APMPackage.from_apm_yml` has
    already succeeded in :func:`run_policy_checks`.  The primary security
    gate is ``from_apm_yml()`` -- if it fails, the audit aborts with a
    ``manifest-parse`` check result and this function is never reached.

    Returning ``None`` here is therefore **defence-in-depth**: it covers
    edge cases (TOCTOU race, transient I/O error) where the file becomes
    unreadable between the two calls.  Callers that receive ``None``
    gracefully skip supplementary raw-field checks (e.g.
    ``compilation-target``, ``extensions-present``) rather than hard-failing.

    Returns ``None`` when the file is absent, unreadable, malformed YAML,
    or not a mapping -- but logs a warning so the failure is visible
    rather than silently swallowed.
    """
    import yaml

    apm_yml_path = project_root / "apm.yml"
    if not apm_yml_path.exists():
        return None
    try:
        with open(apm_yml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        # TOCTOU: file disappeared between exists() check and open(); normal condition.
        return None
    except yaml.YAMLError as exc:
        _logger.warning("Malformed YAML in %s: %s", apm_yml_path, exc)
        return None
    except OSError as exc:
        _logger.warning("Cannot read %s: %s", apm_yml_path, exc)
        return None
    except UnicodeDecodeError as exc:
        _logger.warning("Cannot decode %s as UTF-8: %s", apm_yml_path, exc)
        return None
    if not isinstance(data, dict):
        _logger.warning(
            "apm.yml is not a YAML mapping (got %s) -- skipping raw-field checks",
            type(data).__name__,
        )
        return None
    return data


# -- Individual policy checks --------------------------------------


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

    if not policy.deny:
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
    if not policy.require:
        return CheckResult(
            name="required-packages",
            passed=True,
            message="No required packages configured",
        )

    dep_names = {dep.get_canonical_dependency_string().split("#")[0] for dep in deps}
    missing: list[str] = []
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
    deps: list[DependencyReference],
    lock: LockFile | None,
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 4: required packages appear in lockfile with deployed files."""
    if not policy.require or lock is None:
        return CheckResult(
            name="required-packages-deployed",
            passed=True,
            message="No required packages to verify deployment",
        )

    dep_names = {dep.get_canonical_dependency_string().split("#")[0] for dep in deps}
    lock_by_name = {locked.get_unique_key(): locked for _key, locked in lock.dependencies.items()}
    not_deployed: list[str] = []
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
    deps: list[DependencyReference],
    lock: LockFile | None,
    policy: DependencyPolicy,
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


def _check_mcp_allowlist(
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 7: MCP server names match allow list."""
    from .matcher import check_mcp_allowed

    if policy.allow is None:
        return CheckResult(
            name="mcp-allowlist",
            passed=True,
            message="No MCP allow list configured",
        )

    violations: list[str] = []
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
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 8: no MCP server matches deny list."""
    from .matcher import check_mcp_allowed

    if not policy.deny:
        return CheckResult(
            name="mcp-denylist",
            passed=True,
            message="No MCP deny list configured",
        )

    violations: list[str] = []
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
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 9: MCP transport values match policy allow list."""
    allowed_transports = policy.transport.allow
    if allowed_transports is None:
        return CheckResult(
            name="mcp-transport",
            passed=True,
            message="No MCP transport restrictions configured",
        )

    violations: list[str] = []
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
    mcp_deps: list,
    policy: McpPolicy,
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
    raw_yml: dict | None,
    policy: CompilationPolicy,
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
        allow_set = set(allow) if isinstance(allow, (list, tuple)) else {allow}
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
    raw_yml: dict | None,
    policy: CompilationPolicy,
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
    raw_yml: dict | None,
    policy: CompilationPolicy,
) -> CheckResult:
    """Check 13: source attribution enabled if policy requires."""
    if not policy.source_attribution:
        return CheckResult(
            name="source-attribution",
            passed=True,
            message="Source attribution not required by policy",
        )

    compilation = (raw_yml or {}).get("compilation", {})
    attribution = compilation.get("source_attribution") if isinstance(compilation, dict) else None
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
    raw_yml: dict | None,
    policy: ManifestPolicy,
) -> CheckResult:
    """Check 14: all required fields are present with non-empty values."""
    if not policy.required_fields:
        return CheckResult(
            name="required-manifest-fields",
            passed=True,
            message="No required manifest fields configured",
        )

    data = raw_yml or {}
    missing: list[str] = []
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


_INCLUDES_NOT_PROVIDED = object()


def _check_includes_explicit(
    manifest_includes,
    policy: ManifestPolicy,
) -> CheckResult:
    """Check: manifest declares an explicit ``includes:`` list when policy requires it.

    ``manifest_includes`` is the parsed value of the manifest's ``includes:``
    field as exposed by :class:`APMPackage` -- one of ``None`` (field
    absent), the literal string ``"auto"``, or a list of repo-relative
    path strings.

    Violation when ``policy.require_explicit_includes`` is True and
    ``manifest_includes`` is ``None`` or ``"auto"``.
    """
    if not policy.require_explicit_includes:
        return CheckResult(
            name="explicit-includes",
            passed=True,
            message="Explicit includes not required by policy",
        )

    if manifest_includes is None:
        return CheckResult(
            name="explicit-includes",
            passed=False,
            message=(
                "Policy requires explicit 'includes:' paths but none are "
                "declared. Add 'includes: [<path>, ...]' to apm.yml with "
                "the paths you intend to publish."
            ),
            details=[
                "includes: <absent>, require_explicit_includes: true",
            ],
        )

    if manifest_includes == "auto":
        return CheckResult(
            name="explicit-includes",
            passed=False,
            message=(
                "Policy requires explicit 'includes:' paths but manifest "
                "uses 'includes: auto'. Replace with an explicit list of "
                "paths."
            ),
            details=[
                "includes: 'auto', require_explicit_includes: true",
            ],
        )

    return CheckResult(
        name="explicit-includes",
        passed=True,
        message="Manifest declares explicit includes paths",
    )


def _check_scripts_policy(
    raw_yml: dict | None,
    policy: ManifestPolicy,
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
    lock: LockFile | None,
    policy: UnmanagedFilesPolicy,
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
        When ``None`` target/compilation checks are **skipped** (they
        belong to the separate W2-target-aware call).
    fetch_outcome:
        Human-readable label for diagnostic context (e.g.
        ``"cached"``, ``"fetched"``).  Currently informational only.
    fail_fast:
        Stop after the first failing check (default ``True``).
    manifest_includes:
        The parsed value of the manifest's ``includes:`` field
        (``None``, ``"auto"``, or a list of paths).  When omitted,
        the ``explicit-includes`` check is skipped -- callers that
        do not have manifest information available (e.g. dep-only
        seams) can leave it unset.

    Returns
    -------
    CIAuditResult
        Contains individual :class:`CheckResult` entries.  The caller
        decides how to map ``enforcement`` level (block vs warn) onto
        these results.

    Notes
    -----
    ``require_resolution: project-wins`` semantics (rubber-duck I7):
    version-pin mismatches are downgraded to warnings; missing required
    packages still block; inherited org deny still wins.  This is
    handled inside ``_check_required_package_version`` which already
    reads ``policy.dependencies.require_resolution``.

    Does **not** load ``apm.yml`` from disk -- the caller supplies the
    resolved dep set directly.
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

    # -- MCP checks (7-10) ----------------------------------------
    # When mcp_deps is None (not provided), skip MCP checks entirely.
    # When mcp_deps is an empty list (provided but no MCP deps), still
    # run MCP checks so they report "no X configured" for completeness.
    if mcp_deps is not None:
        if _run(_check_mcp_allowlist(mcp_list, policy.mcp)):
            return result
        if _run(_check_mcp_denylist(mcp_list, policy.mcp)):
            return result
        if _run(_check_mcp_transport(mcp_list, policy.mcp)):
            return result
        if _run(_check_mcp_self_defined(mcp_list, policy.mcp)):
            return result

    # -- Target / compilation checks (11-13) -----------------------
    # Skipped when effective_target is None -- those run in a separate
    # post-targets call (W2-target-aware).
    if effective_target is not None:
        # Build a minimal raw_yml dict so _check_compilation_target
        # sees the effective (possibly CLI-overridden) target value
        # rather than what is literally on disk.
        synthetic_yml = {"target": effective_target}
        if _run(_check_compilation_target(synthetic_yml, policy.compilation)):
            return result

    # -- Manifest-level explicit-includes check --------------------
    # Only run when the caller supplied the manifest includes value.
    # Dep-only seams that lack manifest context (legacy callers) skip
    # this check; the install pipeline and ``apm audit`` wrappers both
    # supply it.
    if manifest_includes is not _INCLUDES_NOT_PROVIDED:
        if _run(_check_includes_explicit(manifest_includes, policy.manifest)):
            return result

    # NOTE: compilation strategy, source attribution, manifest fields,
    # scripts policy, and unmanaged files are disk-level / manifest-level
    # concerns.  They are NOT included in the resolved-dep seam because
    # the install pipeline does not have the raw manifest at this point
    # and they are already covered by the full ``run_policy_checks``
    # wrapper that ``apm audit --ci`` calls.

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
    from ..models.apm_package import APMPackage, clear_apm_yml_cache

    result = CIAuditResult()

    # Load manifest
    apm_yml_path = project_root / "apm.yml"
    if not apm_yml_path.exists():
        return result

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

    # Load lockfile (optional -- some checks work without it)
    lockfile_path = get_lockfile_path(project_root)
    lock = LockFile.read(lockfile_path) if lockfile_path.exists() else None

    # Load raw YAML for field-level checks
    raw_yml = _load_raw_apm_yml(project_root)

    # Get dependencies from manifest (disk view)
    apm_deps = manifest.get_apm_dependencies()
    mcp_deps = manifest.get_mcp_dependencies()

    # Read effective target from raw manifest for the full-project path
    # NOTE: the wrapper does NOT pass effective_target to the dep seam.
    # Target checks run as disk-level checks below (reading raw_yml),
    # because the wrapper has the on-disk manifest.  The install pipeline
    # will pass effective_target directly (W2-target-aware).

    # -- Delegate dependency + MCP checks to shared seam ---------------
    dep_result = run_dependency_policy_checks(
        apm_deps,
        lockfile=lock,
        policy=policy,
        mcp_deps=mcp_deps,
        # effective_target=None: target checks handled below from raw_yml
        fail_fast=fail_fast,
        manifest_includes=manifest.includes,
    )
    result.checks.extend(dep_result.checks)

    # Early exit if dep checks already failed in fail-fast mode
    if fail_fast and not dep_result.passed:
        return result

    def _run(check: CheckResult) -> bool:
        """Append check and return True if fail-fast should stop."""
        result.checks.append(check)
        return fail_fast and not check.passed

    # -- Disk-level checks that only apply to full-project audits --

    # Compilation checks (11-13) -- all run from raw_yml in wrapper
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
    _run(_check_unmanaged_files(project_root, lock, policy.unmanaged_files))

    return result
