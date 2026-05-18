# pylint: disable=duplicate-code
"""Policy checks for organisational governance enforcement.

These checks run WITH a policy file and validate that the project's manifest,
lockfile, and on-disk state comply with the organisation's declared policies.
They are always run in addition to the baseline checks in ``ci_checks``.

Public surface
--------------
All names below are importable from this module and are re-exported unchanged.
Implementations live in cohesive private sibling modules:

* ``_dep_checks``         – checks 1-6 (allow/deny/require/version/depth)
* ``_compilation_checks`` – checks 11-13 (target, strategy, source attribution)
* ``_manifest_checks``    – ``_load_raw_apm_yml`` + checks 14-15 + explicit-includes
* ``_mcp_checks``         – checks 7-10 (MCP server governance)
* ``policy_check_impl``   – check 16 + aggregate runners
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models import CheckResult, CIAuditResult
from ._compilation_checks import (
    _check_compilation_strategy,
    _check_compilation_target,
    _check_source_attribution,
)
from ._dep_checks import (
    _check_dependency_allowlist,
    _check_dependency_denylist,
    _check_required_package_version,
    _check_required_packages,
    _check_required_packages_deployed,
    _check_transitive_depth,
)
from ._manifest_checks import (
    _check_includes_explicit,
    _check_required_manifest_fields,
    _check_scripts_policy,
    _load_raw_apm_yml,
)
from ._mcp_checks import (
    _check_mcp_allowlist,
    _check_mcp_denylist,
    _check_mcp_self_defined,
    _check_mcp_transport,
)
from .class_ import (
    ApmPolicy,
    LockFile,
    UnmanagedFilesPolicy,
)

_INCLUDES_NOT_PROVIDED = object()


@dataclass(frozen=True, slots=True)
class PolicyCheckOpts:
    """Options for run_dependency_policy_checks."""

    lockfile: LockFile | None = None
    mcp_deps: list | None = None
    effective_target: str | None = None
    fetch_outcome: str | None = None
    fail_fast: bool = True
    manifest_includes = _INCLUDES_NOT_PROVIDED


# ---------------------------------------------------------------------------
# Aggregate runners (delegate to policy_check_impl)
# ---------------------------------------------------------------------------


def run_dependency_policy_checks(
    deps_to_install,
    policy: ApmPolicy,
    opts: PolicyCheckOpts | None = None,
    **kwargs,
) -> CIAuditResult:
    """Evaluate policy against a dependency set.

    Parameters
    ----------
    deps_to_install:
        Iterable of ``DependencyReference``.
    policy:
        The effective :class:`ApmPolicy` to enforce.
    opts:
        Optional dataclass with all other parameters. When provided,
        kwargs are ignored.
    **kwargs:
        Backward-compatible parameters: lockfile, mcp_deps,
        effective_target, fetch_outcome, fail_fast, manifest_includes.
    """
    # Resolve opts for backward compatibility
    if opts is not None:
        lockfile = opts.lockfile
        mcp_deps = opts.mcp_deps
        effective_target = opts.effective_target
        fetch_outcome = opts.fetch_outcome
        fail_fast = opts.fail_fast
        manifest_includes = opts.manifest_includes
    else:
        lockfile = kwargs.get("lockfile")
        mcp_deps = kwargs.get("mcp_deps")
        effective_target = kwargs.get("effective_target")
        fetch_outcome = kwargs.get("fetch_outcome")
        fail_fast = kwargs.get("fail_fast", True)
        manifest_includes = kwargs.get("manifest_includes", _INCLUDES_NOT_PROVIDED)

    kwargs_dict = {
        "lockfile": lockfile,
        "policy": policy,
        "mcp_deps": mcp_deps,
        "effective_target": effective_target,
        "fetch_outcome": fetch_outcome,
        "fail_fast": fail_fast,
    }
    if manifest_includes is not _INCLUDES_NOT_PROVIDED:
        kwargs_dict["manifest_includes"] = manifest_includes
    return _policy_check_impl.run_dependency_policy_checks(deps_to_install, **kwargs_dict)


def run_policy_checks(
    project_root: Path, policy: ApmPolicy, *, fail_fast: bool = True
) -> CIAuditResult:
    return _policy_check_impl.run_policy_checks(project_root, policy, fail_fast=fail_fast)


# ---------------------------------------------------------------------------
# Delegation wrapper for _check_unmanaged_files whose implementation lives in
# policy_check_impl (check 16).
# ---------------------------------------------------------------------------


def _check_unmanaged_files(
    project_root: Path, lock: LockFile | None, policy: UnmanagedFilesPolicy
) -> CheckResult:
    return _policy_check_impl._check_unmanaged_files(project_root, lock, policy)


# Deferred import resolves the mutual dependency with policy_check_impl
# (policy_check_impl imports from this module at its top level).
from . import policy_check_impl as _policy_check_impl  # noqa: E402
