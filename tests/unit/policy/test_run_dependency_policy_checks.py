"""Tests for ``run_dependency_policy_checks`` -- the resolved-dep policy seam.

Covers:
- dependency allow / deny / required / required-version checks
- ``project-wins`` semantics (rubber-duck I7): version-pin mismatches
  downgraded to warnings; missing required packages still block;
  inherited org deny still wins
- MCP checks present in the resolved set
- target skipping when ``effective_target is None``
- target enforcement when ``effective_target`` is provided
- fail-fast vs run-all modes
"""

from __future__ import annotations

from typing import List, Optional  # noqa: F401, UP035

import pytest  # noqa: F401

from apm_cli.policy.models import CheckResult, CIAuditResult  # noqa: F401
from apm_cli.policy.policy_checks import run_dependency_policy_checks
from apm_cli.policy.schema import (
    ApmPolicy,
    CompilationPolicy,
    CompilationTargetPolicy,
    DependencyPolicy,
    McpPolicy,
    McpTransportPolicy,
)

# -- Helpers --------------------------------------------------------


def _make_dep_refs(dep_strings: list[str]):
    """Parse a list of dep strings into DependencyReference objects."""
    from apm_cli.models.apm_package import DependencyReference

    return [DependencyReference.parse(s) for s in dep_strings]


def _make_mcp_deps(mcp_list: list):
    """Create MCPDependency objects from dicts or strings."""
    from apm_cli.models.dependency import MCPDependency

    result = []
    for item in mcp_list:
        if isinstance(item, str):
            result.append(MCPDependency.from_string(item))
        elif isinstance(item, dict):
            result.append(MCPDependency.from_dict(item))
    return result


def _make_lockfile(deps_data: list[dict]):
    """Create a LockFile from a list of dependency dicts."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    lock = LockFile()
    for d in deps_data:
        lock.add_dependency(LockedDependency.from_dict(d))
    return lock


def _check_names(result: CIAuditResult) -> list[str]:
    """Return the names of all checks in the result."""
    return [c.name for c in result.checks]


def _failed_names(result: CIAuditResult) -> list[str]:
    """Return the names of all failed checks."""
    return [c.name for c in result.checks if not c.passed]


# -- Dependency allow/deny -----------------------------------------


class TestDependencyAllowDeny:
    def test_pass_no_restrictions(self):
        """Default policy (no allow/deny) passes any deps."""
        deps = _make_dep_refs(["owner/repo", "other/pkg"])
        policy = ApmPolicy()
        result = run_dependency_policy_checks(deps, policy=policy)
        assert result.passed

    def test_allow_list_pass(self):
        """Deps matching allow list pass."""
        deps = _make_dep_refs(["owner/repo"])
        policy = ApmPolicy(dependencies=DependencyPolicy(allow=("owner/*",)))
        result = run_dependency_policy_checks(deps, policy=policy)
        assert result.passed
        assert "dependency-allowlist" in _check_names(result)

    def test_allow_list_fail(self):
        """Deps NOT matching allow list fail."""
        deps = _make_dep_refs(["evil/pkg"])
        policy = ApmPolicy(dependencies=DependencyPolicy(allow=("owner/*",)))
        result = run_dependency_policy_checks(deps, policy=policy)
        assert not result.passed
        assert "dependency-allowlist" in _failed_names(result)

    def test_deny_list_blocks(self):
        """Deps matching deny list fail."""
        deps = _make_dep_refs(["evil/malware"])
        policy = ApmPolicy(dependencies=DependencyPolicy(deny=("evil/*",)))
        result = run_dependency_policy_checks(deps, policy=policy)
        assert not result.passed
        assert "dependency-denylist" in _failed_names(result)

    def test_deny_list_pass(self):
        """Deps NOT matching deny list pass."""
        deps = _make_dep_refs(["good/pkg"])
        policy = ApmPolicy(dependencies=DependencyPolicy(deny=("evil/*",)))
        result = run_dependency_policy_checks(deps, policy=policy)
        assert result.passed

    def test_deny_wins_over_local_allow(self):
        """Inherited org deny wins even when dep would otherwise pass.

        (Rubber-duck I7: inherited org deny still wins over repo-local allow.)
        """
        deps = _make_dep_refs(["evil/malware"])
        policy = ApmPolicy(
            dependencies=DependencyPolicy(
                allow=("evil/*",),  # repo-local might allow
                deny=("evil/*",),  # but org deny wins
            )
        )
        result = run_dependency_policy_checks(deps, policy=policy)
        assert not result.passed
        assert "dependency-denylist" in _failed_names(result)

    def test_empty_deps_passes(self):
        """Empty dep list always passes dependency checks."""
        policy = ApmPolicy(dependencies=DependencyPolicy(allow=("owner/*",), deny=("evil/*",)))
        result = run_dependency_policy_checks([], policy=policy)
        assert result.passed


# -- Required packages ---------------------------------------------


class TestRequiredPackages:
    def test_required_present(self):
        """Required package in resolved set passes."""
        deps = _make_dep_refs(["org/required-pkg"])
        policy = ApmPolicy(dependencies=DependencyPolicy(require=("org/required-pkg",)))
        result = run_dependency_policy_checks(deps, policy=policy)
        # required-packages check should pass
        req_check = [c for c in result.checks if c.name == "required-packages"]
        assert req_check and req_check[0].passed

    def test_required_missing_blocks(self):
        """Missing required package fails (even with project-wins).

        Rubber-duck I7: missing required packages still block.
        """
        deps = _make_dep_refs(["other/pkg"])
        policy = ApmPolicy(
            dependencies=DependencyPolicy(
                require=("org/required-pkg",),
                require_resolution="project-wins",
            )
        )
        result = run_dependency_policy_checks(deps, policy=policy)
        assert not result.passed
        assert "required-packages" in _failed_names(result)

    def test_required_missing_blocks_regardless_of_resolution(self):
        """Missing required packages block for all resolution strategies."""
        for strategy in ("project-wins", "policy-wins", "block"):
            deps = _make_dep_refs(["unrelated/pkg"])
            policy = ApmPolicy(
                dependencies=DependencyPolicy(
                    require=("org/must-have",),
                    require_resolution=strategy,
                )
            )
            result = run_dependency_policy_checks(deps, policy=policy)
            assert not result.passed, f"Expected block for missing required with {strategy}"


# -- Required version + project-wins semantics ---------------------


class TestRequiredVersionProjectWins:
    """Rubber-duck I7: project-wins downgrades version-pin mismatches to
    warnings ONLY.  ``policy-wins`` and ``block`` still fail.
    """

    def _make_lock_with_ref(self, pkg: str, ref: str):
        return _make_lockfile([{"repo_url": pkg, "resolved_ref": ref, "deployed_files": ["f"]}])

    def test_project_wins_version_mismatch_is_warning(self):
        """project-wins: version mismatch is a warning, not a failure."""
        deps = _make_dep_refs(["org/pkg#v1.0.0"])
        lock = self._make_lock_with_ref("org/pkg", "v1.0.0")
        policy = ApmPolicy(
            dependencies=DependencyPolicy(
                require=("org/pkg#v2.0.0",),
                require_resolution="project-wins",
            )
        )
        result = run_dependency_policy_checks(deps, lockfile=lock, policy=policy)
        ver_check = [c for c in result.checks if c.name == "required-package-version"]
        assert ver_check, "expected required-package-version check"
        assert ver_check[0].passed, "project-wins should downgrade version mismatch to warning"
        # But it should have warning details
        assert ver_check[0].details, "should carry warning details"

    def test_policy_wins_version_mismatch_blocks(self):
        """policy-wins: version mismatch fails."""
        deps = _make_dep_refs(["org/pkg#v1.0.0"])
        lock = self._make_lock_with_ref("org/pkg", "v1.0.0")
        policy = ApmPolicy(
            dependencies=DependencyPolicy(
                require=("org/pkg#v2.0.0",),
                require_resolution="policy-wins",
            )
        )
        result = run_dependency_policy_checks(deps, lockfile=lock, policy=policy)
        assert not result.passed
        assert "required-package-version" in _failed_names(result)

    def test_block_resolution_version_mismatch_blocks(self):
        """block resolution: version mismatch fails."""
        deps = _make_dep_refs(["org/pkg#v1.0.0"])
        lock = self._make_lock_with_ref("org/pkg", "v1.0.0")
        policy = ApmPolicy(
            dependencies=DependencyPolicy(
                require=("org/pkg#v2.0.0",),
                require_resolution="block",
            )
        )
        result = run_dependency_policy_checks(deps, lockfile=lock, policy=policy)
        assert not result.passed
        assert "required-package-version" in _failed_names(result)

    def test_project_wins_version_match_passes(self):
        """project-wins: matching version pin passes cleanly."""
        deps = _make_dep_refs(["org/pkg#v2.0.0"])
        lock = self._make_lock_with_ref("org/pkg", "v2.0.0")
        policy = ApmPolicy(
            dependencies=DependencyPolicy(
                require=("org/pkg#v2.0.0",),
                require_resolution="project-wins",
            )
        )
        result = run_dependency_policy_checks(deps, lockfile=lock, policy=policy)
        ver_check = [c for c in result.checks if c.name == "required-package-version"]
        assert ver_check and ver_check[0].passed
        assert not ver_check[0].details  # no warnings


# -- MCP checks in resolved set ------------------------------------


class TestMcpChecksInResolvedSet:
    def test_mcp_allow_pass(self):
        """MCP server in allow list passes."""
        deps = _make_dep_refs(["owner/repo"])
        mcps = _make_mcp_deps(["io.github.good/server"])
        policy = ApmPolicy(mcp=McpPolicy(allow=("io.github.good/*",)))
        result = run_dependency_policy_checks(deps, policy=policy, mcp_deps=mcps)
        assert result.passed
        assert "mcp-allowlist" in _check_names(result)

    def test_mcp_allow_fail(self):
        """MCP server NOT in allow list fails."""
        deps = _make_dep_refs(["owner/repo"])
        mcps = _make_mcp_deps(["io.github.evil/server"])
        policy = ApmPolicy(mcp=McpPolicy(allow=("io.github.good/*",)))
        result = run_dependency_policy_checks(deps, policy=policy, mcp_deps=mcps)
        assert not result.passed
        assert "mcp-allowlist" in _failed_names(result)

    def test_mcp_deny_blocks(self):
        """MCP server matching deny list fails."""
        deps = _make_dep_refs(["owner/repo"])
        mcps = _make_mcp_deps(["io.github.evil/malware"])
        policy = ApmPolicy(mcp=McpPolicy(deny=("io.github.evil/*",)))
        result = run_dependency_policy_checks(deps, policy=policy, mcp_deps=mcps)
        assert not result.passed
        assert "mcp-denylist" in _failed_names(result)

    def test_mcp_transport_restriction(self):
        """MCP transport not in allowed list fails."""
        deps = _make_dep_refs(["owner/repo"])
        mcps = _make_mcp_deps([{"name": "evil-server", "transport": "http"}])
        policy = ApmPolicy(mcp=McpPolicy(transport=McpTransportPolicy(allow=("stdio",))))
        result = run_dependency_policy_checks(deps, policy=policy, mcp_deps=mcps)
        assert not result.passed
        assert "mcp-transport" in _failed_names(result)

    def test_mcp_self_defined_deny(self):
        """Self-defined MCP server fails when policy denies."""
        deps = _make_dep_refs(["owner/repo"])
        mcps = _make_mcp_deps(
            [{"name": "my-server", "registry": False, "transport": "stdio", "command": "node"}]
        )
        policy = ApmPolicy(mcp=McpPolicy(self_defined="deny"))
        result = run_dependency_policy_checks(deps, policy=policy, mcp_deps=mcps)
        assert not result.passed
        assert "mcp-self-defined" in _failed_names(result)

    def test_no_mcp_deps_skips_mcp_checks(self):
        """When mcp_deps is None (default), MCP checks are skipped entirely."""
        deps = _make_dep_refs(["owner/repo"])
        policy = ApmPolicy(mcp=McpPolicy(allow=("strict/*",), deny=("evil/*",)))
        # mcp_deps not passed (default None)
        result = run_dependency_policy_checks(deps, policy=policy)
        assert result.passed
        mcp_check_names = [c.name for c in result.checks if c.name.startswith("mcp-")]
        assert mcp_check_names == [], "MCP checks should be skipped when mcp_deps is None"

    def test_empty_mcp_deps_runs_mcp_checks(self):
        """When mcp_deps is [] (explicitly empty), MCP checks still run."""
        deps = _make_dep_refs(["owner/repo"])
        policy = ApmPolicy(mcp=McpPolicy(allow=("strict/*",)))
        # Explicitly pass empty list
        result = run_dependency_policy_checks(deps, policy=policy, mcp_deps=[])
        assert result.passed
        mcp_check_names = [c.name for c in result.checks if c.name.startswith("mcp-")]
        assert len(mcp_check_names) == 4, (
            "MCP checks should run when mcp_deps=[] (explicitly provided)"
        )


# -- Target / compilation checks -----------------------------------


class TestTargetChecks:
    def test_target_skipped_when_none(self):
        """effective_target=None skips compilation-target check."""
        deps = _make_dep_refs(["owner/repo"])
        policy = ApmPolicy(
            compilation=CompilationPolicy(target=CompilationTargetPolicy(allow=("vscode",)))
        )
        result = run_dependency_policy_checks(deps, policy=policy, effective_target=None)
        assert result.passed
        assert "compilation-target" not in _check_names(result)

    def test_target_enforced_when_provided(self):
        """effective_target='claude' with allow=[vscode] fails."""
        deps = _make_dep_refs(["owner/repo"])
        policy = ApmPolicy(
            compilation=CompilationPolicy(target=CompilationTargetPolicy(allow=("vscode",)))
        )
        result = run_dependency_policy_checks(deps, policy=policy, effective_target="claude")
        assert not result.passed
        assert "compilation-target" in _failed_names(result)

    def test_target_pass_when_allowed(self):
        """effective_target='vscode' with allow=[vscode] passes."""
        deps = _make_dep_refs(["owner/repo"])
        policy = ApmPolicy(
            compilation=CompilationPolicy(target=CompilationTargetPolicy(allow=("vscode",)))
        )
        result = run_dependency_policy_checks(deps, policy=policy, effective_target="vscode")
        assert result.passed
        assert "compilation-target" in _check_names(result)


# -- Fail-fast vs run-all ------------------------------------------


class TestFailFast:
    def test_fail_fast_stops_early(self):
        """fail_fast=True stops after first failure."""
        deps = _make_dep_refs(["evil/pkg", "other/missing"])
        policy = ApmPolicy(
            dependencies=DependencyPolicy(
                deny=("evil/*",),
                require=("org/must-have",),
            )
        )
        result = run_dependency_policy_checks(deps, policy=policy, fail_fast=True)
        assert not result.passed
        # Should stop at the first failure (denylist), not reach required
        failed = _failed_names(result)
        assert "dependency-denylist" in failed
        assert "required-packages" not in _check_names(result)

    def test_run_all_continues_after_failure(self):
        """fail_fast=False runs all checks even after failures."""
        deps = _make_dep_refs(["evil/pkg"])
        policy = ApmPolicy(
            dependencies=DependencyPolicy(
                deny=("evil/*",),
                require=("org/must-have",),
            )
        )
        result = run_dependency_policy_checks(deps, policy=policy, fail_fast=False)
        assert not result.passed
        # Both denylist and required-packages checks should run
        names = _check_names(result)
        assert "dependency-denylist" in names
        assert "required-packages" in names


# -- Disk-level checks NOT included --------------------------------


class TestDiskChecksExcluded:
    """Verify that disk-level checks (compilation strategy, source
    attribution, manifest fields, scripts, unmanaged files) are NOT
    run by the dep seam.
    """

    def test_no_disk_checks_in_dep_seam(self):
        deps = _make_dep_refs(["owner/repo"])
        policy = ApmPolicy()  # default: no restrictions
        result = run_dependency_policy_checks(deps, policy=policy)
        disk_check_names = {
            "compilation-strategy",
            "source-attribution",
            "required-manifest-fields",
            "scripts-policy",
            "unmanaged-files",
        }
        found = disk_check_names & set(_check_names(result))
        assert not found, f"Disk-level checks should not appear: {found}"


# -- Mixed scenario: multiple checks with project-wins combo -------


class TestCombinedProjectWinsScenario:
    """End-to-end scenario testing the full project-wins semantics:
    - deny still blocks
    - missing required still blocks
    - version mismatch is a warning only
    """

    def test_deny_wins_despite_project_wins(self):
        """Even with project-wins, a denied dep is blocked."""
        deps = _make_dep_refs(["evil/malware", "org/required-pkg#v1.0.0"])
        lock = _make_lockfile(
            [
                {
                    "repo_url": "org/required-pkg",
                    "resolved_ref": "v1.0.0",
                    "deployed_files": ["f"],
                },
            ]
        )
        policy = ApmPolicy(
            dependencies=DependencyPolicy(
                deny=("evil/*",),
                require=("org/required-pkg#v2.0.0",),
                require_resolution="project-wins",
            )
        )
        result = run_dependency_policy_checks(deps, lockfile=lock, policy=policy, fail_fast=False)
        assert not result.passed
        failed = _failed_names(result)
        assert "dependency-denylist" in failed

    def test_project_wins_full_pass(self):
        """With project-wins: no deny, required present, version mismatch
        is a warning -- overall result passes.
        """
        deps = _make_dep_refs(["org/pkg#v1.0.0"])
        lock = _make_lockfile(
            [
                {
                    "repo_url": "org/pkg",
                    "resolved_ref": "v1.0.0",
                    "deployed_files": ["f"],
                },
            ]
        )
        policy = ApmPolicy(
            dependencies=DependencyPolicy(
                require=("org/pkg#v2.0.0",),
                require_resolution="project-wins",
            )
        )
        result = run_dependency_policy_checks(deps, lockfile=lock, policy=policy, fail_fast=False)
        # Overall should pass (version mismatch is warning only)
        assert result.passed
        # But the version check should carry warning details
        ver_check = [c for c in result.checks if c.name == "required-package-version"]
        assert ver_check and ver_check[0].details


class TestExplicitIncludesSeam:
    """Wiring of the explicit-includes check into run_dependency_policy_checks.

    Covers the sentinel behaviour: when the caller does not supply
    ``manifest_includes`` the check is skipped (preserves legacy
    callers that lack manifest context); when the caller supplies it
    the check runs against ``policy.manifest.require_explicit_includes``.
    """

    def _policy(self, *, require: bool):
        from apm_cli.policy.schema import ManifestPolicy

        return ApmPolicy(manifest=ManifestPolicy(require_explicit_includes=require))

    def test_skipped_when_manifest_includes_not_provided(self):
        # Default: no manifest_includes kwarg -> no explicit-includes
        # CheckResult appears in the result.
        result = run_dependency_policy_checks([], policy=self._policy(require=True))
        assert "explicit-includes" not in _check_names(result)

    def test_violation_when_required_and_includes_none(self):
        result = run_dependency_policy_checks(
            [],
            policy=self._policy(require=True),
            manifest_includes=None,
            fail_fast=False,
        )
        assert "explicit-includes" in _failed_names(result)

    def test_violation_when_required_and_includes_auto(self):
        result = run_dependency_policy_checks(
            [],
            policy=self._policy(require=True),
            manifest_includes="auto",
            fail_fast=False,
        )
        assert "explicit-includes" in _failed_names(result)

    def test_pass_when_required_and_includes_explicit_list(self):
        result = run_dependency_policy_checks(
            [],
            policy=self._policy(require=True),
            manifest_includes=["a.md", "b.md"],
            fail_fast=False,
        )
        assert "explicit-includes" in _check_names(result)
        assert "explicit-includes" not in _failed_names(result)

    def test_pass_when_not_required_regardless_of_includes(self):
        for value in (None, "auto", ["a.md"]):
            result = run_dependency_policy_checks(
                [],
                policy=self._policy(require=False),
                manifest_includes=value,
                fail_fast=False,
            )
            assert "explicit-includes" not in _failed_names(result), (
                f"unexpected violation for includes={value!r}"
            )
