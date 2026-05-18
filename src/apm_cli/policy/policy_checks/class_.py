"""Policy checks for organisational governance enforcement.

These checks run WITH a policy file and validate that the project's manifest,
lockfile, and on-disk state comply with the organisation's declared policies.
They are always run in addition to the baseline checks in ``ci_checks``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from apm_cli.deps.lockfile import LockFile
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.policy.schema import (
    ApmPolicy,
    CompilationPolicy,
    DependencyPolicy,
    ManifestPolicy,
    McpPolicy,
    UnmanagedFilesPolicy,
)

from ..models import CheckResult, CIAuditResult

_logger = logging.getLogger(__name__)


# -- Helpers -------------------------------------------------------


def _load_raw_apm_yml(project_root: Path) -> dict | None:
    return _dependency_checks._load_raw_apm_yml(project_root)


# -- Individual policy checks --------------------------------------


def _check_dependency_allowlist(
    deps: list[DependencyReference], policy: DependencyPolicy
) -> CheckResult:
    return _dependency_checks._check_dependency_allowlist(deps, policy)


def _check_dependency_denylist(
    deps: list[DependencyReference], policy: DependencyPolicy
) -> CheckResult:
    return _dependency_checks._check_dependency_denylist(deps, policy)


def _check_required_packages(
    deps: list[DependencyReference], policy: DependencyPolicy
) -> CheckResult:
    return _dependency_checks._check_required_packages(deps, policy)


def _check_required_packages_deployed(
    deps: list[DependencyReference], lock: LockFile | None, policy: DependencyPolicy
) -> CheckResult:
    return _dependency_checks._check_required_packages_deployed(deps, lock, policy)


def _check_required_package_version(
    deps: list[DependencyReference], lock: LockFile | None, policy: DependencyPolicy
) -> CheckResult:
    return _dependency_checks._check_required_package_version(deps, lock, policy)


def _check_transitive_depth(lock: LockFile | None, policy: DependencyPolicy) -> CheckResult:
    return _dependency_checks._check_transitive_depth(lock, policy)


def _check_mcp_allowlist(mcp_deps: list, policy: McpPolicy) -> CheckResult:
    return _dependency_checks._check_mcp_allowlist(mcp_deps, policy)


def _check_mcp_denylist(mcp_deps: list, policy: McpPolicy) -> CheckResult:
    return _dependency_checks._check_mcp_denylist(mcp_deps, policy)


def _check_mcp_transport(mcp_deps: list, policy: McpPolicy) -> CheckResult:
    return _dependency_checks._check_mcp_transport(mcp_deps, policy)


def _check_mcp_self_defined(mcp_deps: list, policy: McpPolicy) -> CheckResult:
    return _dependency_checks._check_mcp_self_defined(mcp_deps, policy)


def _check_compilation_target(raw_yml: dict | None, policy: CompilationPolicy) -> CheckResult:
    return _dependency_checks._check_compilation_target(raw_yml, policy)


def _check_compilation_strategy(raw_yml: dict | None, policy: CompilationPolicy) -> CheckResult:
    return _dependency_checks._check_compilation_strategy(raw_yml, policy)


def _check_source_attribution(raw_yml: dict | None, policy: CompilationPolicy) -> CheckResult:
    return _dependency_checks._check_source_attribution(raw_yml, policy)


def _check_required_manifest_fields(raw_yml: dict | None, policy: ManifestPolicy) -> CheckResult:
    return _dependency_checks._check_required_manifest_fields(raw_yml, policy)


_INCLUDES_NOT_PROVIDED = object()


def _check_includes_explicit(manifest_includes, policy: ManifestPolicy) -> CheckResult:
    return _dependency_checks._check_includes_explicit(manifest_includes, policy)


def _check_scripts_policy(raw_yml: dict | None, policy: ManifestPolicy) -> CheckResult:
    return _dependency_checks._check_scripts_policy(raw_yml, policy)


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
    project_root: Path, lock: LockFile | None, policy: UnmanagedFilesPolicy
) -> CheckResult:
    return _dependency_checks._check_unmanaged_files(project_root, lock, policy)


# -- Aggregate runners ---------------------------------------------


def run_dependency_policy_checks(
    deps_to_install,
    *,
    policy: ApmPolicy,
    **opts,
) -> CIAuditResult:
    return _dependency_checks.run_dependency_policy_checks(deps_to_install, policy=policy, **opts)


def run_policy_checks(
    project_root: Path, policy: ApmPolicy, *, fail_fast: bool = True
) -> CIAuditResult:
    return _dependency_checks.run_policy_checks(project_root, policy, fail_fast=fail_fast)


from . import dependency_checks as _dependency_checks
