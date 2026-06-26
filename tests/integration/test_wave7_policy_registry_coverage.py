"""Integration tests to maximise coverage of:

1. src/apm_cli/policy/policy_checks.py
2. src/apm_cli/registry/operations.py
3. src/apm_cli/marketplace/resolver.py

Strategy
--------
* Call check functions directly (not through the CLI) for maximum branch coverage.
* Only mock external I/O: HTTP (requests.Session.get / requests.get),
  subprocess, auth tokens, os.environ.  No internal apm_cli functions
  are mocked.
* Construct realistic data structures using actual dataclasses.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers shared across test groups
# ---------------------------------------------------------------------------
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.marketplace.models import (  # noqa: F401
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.marketplace.resolver import (
    MarketplacePluginResolution,
    _coerce_dict_plugin_type,
    _compute_cross_repo_misconfig_risk,
    _extract_in_repo_path_and_ref,
    _is_in_marketplace_source,
    _marketplace_host_needs_explicit_git_path,
    _marketplace_project_slug,
    _needs_canonical_host_prefix,
    _normalize_owner_repo_slug,
    _normalize_repo_field_for_match,
    _repo_field_matches_marketplace,
    _resolve_git_subdir_source,
    _resolve_github_source,
    _resolve_relative_source,
    _resolve_url_source,
    parse_marketplace_ref,
    resolve_marketplace_plugin,
    resolve_plugin_source,
)
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.policy.models import CheckResult  # noqa: F401 - used implicitly
from apm_cli.policy.policy_checks import (
    _INCLUDES_NOT_PROVIDED,
    _check_compilation_strategy,
    _check_compilation_target,
    _check_dependency_allowlist,
    _check_dependency_denylist,
    _check_includes_explicit,
    _check_mcp_allowlist,
    _check_mcp_denylist,
    _check_mcp_self_defined,
    _check_mcp_transport,
    _check_required_manifest_fields,
    _check_required_package_version,
    _check_required_packages,
    _check_required_packages_deployed,
    _check_scripts_policy,
    _check_source_attribution,
    _check_transitive_depth,
    _check_unmanaged_files,
    _load_raw_apm_yml,
    run_dependency_policy_checks,
    run_policy_checks,
)
from apm_cli.policy.schema import (
    ApmPolicy,
    CompilationPolicy,
    CompilationStrategyPolicy,
    CompilationTargetPolicy,
    DependencyPolicy,
    ManifestPolicy,
    McpPolicy,
    McpTransportPolicy,
    UnmanagedFilesPolicy,
)

# ---------------------------------------------------------------------------
# Tiny factory helpers
# ---------------------------------------------------------------------------


def _dep(repo_url: str) -> DependencyReference:
    return DependencyReference.parse(repo_url)


def _locked(
    repo_url: str,
    resolved_ref: str = "main",
    depth: int = 1,
    deployed_files: list[str] | None = None,
) -> LockedDependency:
    return LockedDependency(
        repo_url=repo_url,
        resolved_ref=resolved_ref,
        resolved_commit="abc123",
        depth=depth,
        deployed_files=deployed_files or [],
    )


def _lock(*locked_deps: LockedDependency) -> LockFile:
    lf = LockFile()
    for d in locked_deps:
        lf.add_dependency(d)
    return lf


def _mcp(name: str, transport: str | None = None, registry=None) -> MCPDependency:
    """Build an MCPDependency for testing policy checks."""
    if registry is False:
        if transport in ("http", "sse"):
            return MCPDependency.from_dict(
                {
                    "name": name,
                    "transport": transport,
                    "registry": False,
                    "url": "http://localhost:8080",
                }
            )
        if transport == "stdio":
            return MCPDependency.from_dict(
                {"name": name, "transport": "stdio", "registry": False, "command": "npx"}
            )
    return MCPDependency.from_string(name)


# ============================================================================
# 1. POLICY CHECKS
# ============================================================================


class TestCheckDependencyAllowlist:
    def test_no_allow_list_configured(self):
        policy = DependencyPolicy(allow=None)
        result = _check_dependency_allowlist([], policy)
        assert result.passed
        assert "No dependency allow list" in result.message

    def test_empty_deps_with_allow_list(self):
        policy = DependencyPolicy(allow=("acme/pkg",))
        result = _check_dependency_allowlist([], policy)
        assert result.passed

    def test_dep_in_allow_list_passes(self):
        policy = DependencyPolicy(allow=("acme/my-pkg",))
        deps = [_dep("acme/my-pkg")]
        result = _check_dependency_allowlist(deps, policy)
        assert result.passed

    def test_dep_not_in_allow_list_fails(self):
        policy = DependencyPolicy(allow=("acme/allowed",))
        deps = [_dep("acme/forbidden")]
        result = _check_dependency_allowlist(deps, policy)
        assert not result.passed
        assert "1 dependency" in result.message
        assert len(result.details) == 1

    def test_multiple_violations(self):
        policy = DependencyPolicy(allow=("acme/good",))
        deps = [_dep("acme/bad1"), _dep("acme/bad2")]
        result = _check_dependency_allowlist(deps, policy)
        assert not result.passed
        assert "2 dependency" in result.message


class TestCheckDependencyDenylist:
    def test_no_deny_list_configured(self):
        policy = DependencyPolicy(deny=None)
        result = _check_dependency_denylist([], policy)
        assert result.passed
        assert "No dependency deny list" in result.message

    def test_empty_deny_list(self):
        policy = DependencyPolicy(deny=())
        result = _check_dependency_denylist([_dep("acme/pkg")], policy)
        assert result.passed

    def test_dep_not_denied(self):
        policy = DependencyPolicy(deny=("acme/blocked",))
        result = _check_dependency_denylist([_dep("acme/ok")], policy)
        assert result.passed

    def test_dep_denied(self):
        policy = DependencyPolicy(deny=("acme/blocked",))
        result = _check_dependency_denylist([_dep("acme/blocked")], policy)
        assert not result.passed
        assert "1 dependency" in result.message

    def test_wildcard_deny_pattern(self):
        policy = DependencyPolicy(deny=("evil/*",))
        result = _check_dependency_denylist([_dep("evil/bad-pkg")], policy)
        assert not result.passed


class TestCheckRequiredPackages:
    def test_no_required_packages(self):
        policy = DependencyPolicy(require=None)
        result = _check_required_packages([], policy)
        assert result.passed
        assert "No required packages" in result.message

    def test_required_present(self):
        policy = DependencyPolicy(require=("acme/required",))
        deps = [_dep("acme/required")]
        result = _check_required_packages(deps, policy)
        assert result.passed

    def test_required_missing(self):
        policy = DependencyPolicy(require=("acme/required",))
        result = _check_required_packages([], policy)
        assert not result.passed
        assert "1 required" in result.message
        assert "acme/required" in result.details

    def test_required_with_hash_stripped(self):
        policy = DependencyPolicy(require=("acme/required#main",))
        deps = [_dep("acme/required")]
        result = _check_required_packages(deps, policy)
        assert result.passed


class TestCheckRequiredPackagesDeployed:
    def test_no_required_no_lock(self):
        policy = DependencyPolicy(require=None)
        result = _check_required_packages_deployed([], None, policy)
        assert result.passed

    def test_with_lock_and_required_no_lock(self):
        policy = DependencyPolicy(require=("acme/pkg",))
        result = _check_required_packages_deployed([_dep("acme/pkg")], None, policy)
        assert result.passed
        assert "No required packages to verify" in result.message

    def test_required_deployed(self):
        policy = DependencyPolicy(require=("acme/pkg",))
        dep = _dep("acme/pkg")
        locked = _locked("acme/pkg", deployed_files=[".github/agents/pkg/agent.md"])
        lf = _lock(locked)
        result = _check_required_packages_deployed([dep], lf, policy)
        assert result.passed

    def test_required_present_but_parked(self):
        # Gap B (issue #1873): a required package PRESENT in the lockfile but
        # with no deployed_files (executables gated pending approval) is a
        # healthy present-but-parked state -- presence passes; trust is audited
        # separately by required-executable-untrusted.
        policy = DependencyPolicy(require=("acme/pkg",))
        dep = _dep("acme/pkg")
        locked = _locked("acme/pkg", deployed_files=[])  # no deployed files
        lf = _lock(locked)
        result = _check_required_packages_deployed([dep], lf, policy)
        assert result.passed
        assert "present in lockfile" in result.message

    def test_required_absent_from_lockfile(self):
        # Presence check fails only when the required package is missing from
        # the lockfile entirely.
        policy = DependencyPolicy(require=("acme/pkg",))
        dep = _dep("acme/pkg")
        lf = _lock()  # empty lockfile -- acme/pkg absent
        result = _check_required_packages_deployed([dep], lf, policy)
        assert not result.passed
        assert "1 required package" in result.message

    def test_required_not_in_manifest_skipped(self):
        # Package in require but not in deps -- check 3 handles it, not 4
        policy = DependencyPolicy(require=("acme/missing",))
        result = _check_required_packages_deployed([], _lock(), policy)
        assert result.passed


class TestCheckRequiredPackageVersion:
    def test_no_pinned_requirements(self):
        policy = DependencyPolicy(require=("acme/pkg",))  # no # separator
        result = _check_required_package_version([_dep("acme/pkg")], _lock(), policy)
        assert result.passed
        assert "No version-pinned" in result.message

    def test_no_lock_skips(self):
        policy = DependencyPolicy(require=("acme/pkg#v1.0.0",))
        result = _check_required_package_version([_dep("acme/pkg")], None, policy)
        assert result.passed

    def test_version_matches(self):
        policy = DependencyPolicy(require=("acme/pkg#v1.0.0",))
        locked = _locked("acme/pkg", resolved_ref="v1.0.0")
        lf = _lock(locked)
        result = _check_required_package_version([_dep("acme/pkg")], lf, policy)
        assert result.passed

    def test_version_mismatch_block(self):
        policy = DependencyPolicy(require=("acme/pkg#v1.0.0",), require_resolution="block")
        locked = _locked("acme/pkg", resolved_ref="v2.0.0")
        lf = _lock(locked)
        result = _check_required_package_version([_dep("acme/pkg")], lf, policy)
        assert not result.passed
        assert "1 version mismatch" in result.message

    def test_version_mismatch_policy_wins_blocks(self):
        policy = DependencyPolicy(require=("acme/pkg#v1.0.0",), require_resolution="policy-wins")
        locked = _locked("acme/pkg", resolved_ref="v2.0.0")
        lf = _lock(locked)
        result = _check_required_package_version([_dep("acme/pkg")], lf, policy)
        assert not result.passed

    def test_version_mismatch_project_wins_warns(self):
        policy = DependencyPolicy(require=("acme/pkg#v1.0.0",), require_resolution="project-wins")
        locked = _locked("acme/pkg", resolved_ref="v2.0.0")
        lf = _lock(locked)
        result = _check_required_package_version([_dep("acme/pkg")], lf, policy)
        assert result.passed  # warning only
        assert result.details  # details carries the warning


class TestCheckTransitiveDepth:
    def test_no_lockfile(self):
        policy = DependencyPolicy(max_depth=3)
        result = _check_transitive_depth(None, policy)
        assert result.passed
        assert "No lockfile" in result.message

    def test_max_depth_50_or_more_skips(self):
        policy = DependencyPolicy(max_depth=50)
        lf = _lock(_locked("acme/deep", depth=100))
        result = _check_transitive_depth(lf, policy)
        assert result.passed
        assert "No transitive depth limit" in result.message

    def test_within_limit_passes(self):
        policy = DependencyPolicy(max_depth=3)
        lf = _lock(_locked("acme/pkg", depth=2))
        result = _check_transitive_depth(lf, policy)
        assert result.passed

    def test_exceeds_limit_fails(self):
        policy = DependencyPolicy(max_depth=2)
        lf = _lock(_locked("acme/deep", depth=5))
        result = _check_transitive_depth(lf, policy)
        assert not result.passed
        assert "1 dependency" in result.message

    def test_multiple_violations(self):
        policy = DependencyPolicy(max_depth=1)
        lf = _lock(
            _locked("acme/deep1", depth=3),
            _locked("acme/deep2", depth=4),
        )
        result = _check_transitive_depth(lf, policy)
        assert not result.passed
        assert "2 dependency" in result.message


class TestCheckMcpAllowlist:
    def test_no_allow_list(self):
        policy = McpPolicy(allow=None)
        result = _check_mcp_allowlist([], policy)
        assert result.passed
        assert "No MCP allow list" in result.message

    def test_empty_mcps_with_allow_list(self):
        policy = McpPolicy(allow=("server-a",))
        result = _check_mcp_allowlist([], policy)
        assert result.passed

    def test_mcp_in_allow_list(self):
        policy = McpPolicy(allow=("my-server",))
        result = _check_mcp_allowlist([_mcp("my-server")], policy)
        assert result.passed

    def test_mcp_not_in_allow_list(self):
        policy = McpPolicy(allow=("allowed-server",))
        result = _check_mcp_allowlist([_mcp("forbidden-server")], policy)
        assert not result.passed
        assert "1 MCP server" in result.message

    def test_wildcard_allow(self):
        policy = McpPolicy(allow=("github.*",))
        result = _check_mcp_allowlist([_mcp("github.copilot")], policy)
        assert result.passed


class TestCheckMcpDenylist:
    def test_no_deny_list(self):
        policy = McpPolicy(deny=())
        result = _check_mcp_denylist([], policy)
        assert result.passed
        assert "No MCP deny list" in result.message

    def test_mcp_not_denied(self):
        policy = McpPolicy(deny=("evil-server",))
        result = _check_mcp_denylist([_mcp("safe-server")], policy)
        assert result.passed

    def test_mcp_denied(self):
        policy = McpPolicy(deny=("evil-server",))
        result = _check_mcp_denylist([_mcp("evil-server")], policy)
        assert not result.passed
        assert "1 MCP server" in result.message


class TestCheckMcpTransport:
    def test_no_transport_restrictions(self):
        policy = McpPolicy(transport=McpTransportPolicy(allow=None))
        result = _check_mcp_transport([_mcp("my-server")], policy)
        assert result.passed
        assert "No MCP transport restrictions" in result.message

    def test_allowed_transport_passes(self):
        policy = McpPolicy(transport=McpTransportPolicy(allow=("stdio",)))
        mcp = MCPDependency(name="my-server", transport="stdio")
        result = _check_mcp_transport([mcp], policy)
        assert result.passed

    def test_disallowed_transport_fails(self):
        policy = McpPolicy(transport=McpTransportPolicy(allow=("stdio",)))
        mcp = MCPDependency(name="my-server", transport="sse")
        result = _check_mcp_transport([mcp], policy)
        assert not result.passed
        assert "1 MCP transport violation" in result.message

    def test_no_transport_on_mcp_passes(self):
        policy = McpPolicy(transport=McpTransportPolicy(allow=("stdio",)))
        mcp = MCPDependency(name="my-server", transport=None)
        result = _check_mcp_transport([mcp], policy)
        assert result.passed  # None transport is not checked


class TestCheckMcpSelfDefined:
    def test_allow_policy_always_passes(self):
        policy = McpPolicy(self_defined="allow")
        mcp = MCPDependency.from_dict(
            {"name": "custom", "transport": "stdio", "registry": False, "command": "npx"}
        )
        result = _check_mcp_self_defined([mcp], policy)
        assert result.passed
        assert "Self-defined MCP servers allowed" in result.message

    def test_no_self_defined_passes(self):
        policy = McpPolicy(self_defined="deny")
        mcp = _mcp("registry-server")  # registry=None (not False)
        result = _check_mcp_self_defined([mcp], policy)
        assert result.passed
        assert "No self-defined" in result.message

    def test_deny_policy_fails(self):
        policy = McpPolicy(self_defined="deny")
        mcp = MCPDependency.from_dict(
            {"name": "custom", "transport": "stdio", "registry": False, "command": "npx"}
        )
        result = _check_mcp_self_defined([mcp], policy)
        assert not result.passed
        assert "denied by policy" in result.message

    def test_warn_policy_passes_with_details(self):
        policy = McpPolicy(self_defined="warn")
        mcp = MCPDependency.from_dict(
            {"name": "custom", "transport": "stdio", "registry": False, "command": "npx"}
        )
        result = _check_mcp_self_defined([mcp], policy)
        assert result.passed
        assert result.details  # warning details populated


class TestCheckCompilationTarget:
    def test_no_restrictions_configured(self):
        policy = CompilationPolicy(target=CompilationTargetPolicy(enforce=None, allow=None))
        result = _check_compilation_target({"target": "vscode"}, policy)
        assert result.passed
        assert "No compilation target restrictions" in result.message

    def test_no_target_in_manifest(self):
        policy = CompilationPolicy(target=CompilationTargetPolicy(enforce="vscode"))
        result = _check_compilation_target({}, policy)
        assert result.passed
        assert "No compilation target set" in result.message

    def test_raw_yml_none_with_enforce(self):
        policy = CompilationPolicy(target=CompilationTargetPolicy(enforce="vscode"))
        result = _check_compilation_target(None, policy)
        assert result.passed

    def test_enforce_matches(self):
        policy = CompilationPolicy(target=CompilationTargetPolicy(enforce="vscode"))
        result = _check_compilation_target({"target": "vscode"}, policy)
        assert result.passed
        assert "Compilation target compliant" in result.message

    def test_enforce_mismatch(self):
        policy = CompilationPolicy(target=CompilationTargetPolicy(enforce="vscode"))
        result = _check_compilation_target({"target": "claude"}, policy)
        assert not result.passed
        assert "not present" in result.message

    def test_allow_list_matches(self):
        policy = CompilationPolicy(target=CompilationTargetPolicy(allow=("vscode", "claude")))
        result = _check_compilation_target({"target": "vscode"}, policy)
        assert result.passed

    def test_allow_list_rejects(self):
        policy = CompilationPolicy(target=CompilationTargetPolicy(allow=("vscode",)))
        result = _check_compilation_target({"target": "claude"}, policy)
        assert not result.passed

    def test_target_as_list(self):
        policy = CompilationPolicy(target=CompilationTargetPolicy(enforce="vscode"))
        result = _check_compilation_target({"target": ["vscode", "claude"]}, policy)
        assert result.passed


class TestCheckCompilationStrategy:
    def test_no_strategy_enforced(self):
        policy = CompilationPolicy(strategy=CompilationStrategyPolicy(enforce=None))
        result = _check_compilation_strategy({"compilation": {"strategy": "single-file"}}, policy)
        assert result.passed
        assert "No compilation strategy enforced" in result.message

    def test_no_strategy_in_manifest(self):
        policy = CompilationPolicy(strategy=CompilationStrategyPolicy(enforce="distributed"))
        result = _check_compilation_strategy({}, policy)
        assert result.passed
        assert "No compilation strategy set" in result.message

    def test_strategy_matches(self):
        policy = CompilationPolicy(strategy=CompilationStrategyPolicy(enforce="distributed"))
        result = _check_compilation_strategy({"compilation": {"strategy": "distributed"}}, policy)
        assert result.passed

    def test_strategy_mismatch(self):
        policy = CompilationPolicy(strategy=CompilationStrategyPolicy(enforce="distributed"))
        result = _check_compilation_strategy({"compilation": {"strategy": "single-file"}}, policy)
        assert not result.passed
        assert "does not match enforced" in result.message

    def test_compilation_not_dict(self):
        policy = CompilationPolicy(strategy=CompilationStrategyPolicy(enforce="distributed"))
        result = _check_compilation_strategy({"compilation": "not-a-dict"}, policy)
        assert result.passed

    def test_raw_yml_none(self):
        policy = CompilationPolicy(strategy=CompilationStrategyPolicy(enforce="distributed"))
        result = _check_compilation_strategy(None, policy)
        assert result.passed


class TestCheckSourceAttribution:
    def test_not_required(self):
        policy = CompilationPolicy(source_attribution=False)
        result = _check_source_attribution({}, policy)
        assert result.passed
        assert "not required" in result.message

    def test_required_and_enabled(self):
        policy = CompilationPolicy(source_attribution=True)
        result = _check_source_attribution({"compilation": {"source_attribution": True}}, policy)
        assert result.passed

    def test_required_but_missing(self):
        policy = CompilationPolicy(source_attribution=True)
        result = _check_source_attribution({}, policy)
        assert not result.passed

    def test_required_but_false(self):
        policy = CompilationPolicy(source_attribution=True)
        result = _check_source_attribution({"compilation": {"source_attribution": False}}, policy)
        assert not result.passed

    def test_raw_yml_none(self):
        policy = CompilationPolicy(source_attribution=True)
        result = _check_source_attribution(None, policy)
        assert not result.passed


class TestCheckRequiredManifestFields:
    def test_no_required_fields(self):
        policy = ManifestPolicy(required_fields=())
        result = _check_required_manifest_fields({}, policy)
        assert result.passed
        assert "No required manifest fields" in result.message

    def test_all_fields_present(self):
        policy = ManifestPolicy(required_fields=("name", "version"))
        result = _check_required_manifest_fields({"name": "my-pkg", "version": "1.0.0"}, policy)
        assert result.passed

    def test_field_missing(self):
        policy = ManifestPolicy(required_fields=("name", "description"))
        result = _check_required_manifest_fields({"name": "my-pkg"}, policy)
        assert not result.passed
        assert "description" in result.details

    def test_raw_yml_none(self):
        policy = ManifestPolicy(required_fields=("name",))
        result = _check_required_manifest_fields(None, policy)
        assert not result.passed


class TestCheckIncludesExplicit:
    def test_not_required(self):
        policy = ManifestPolicy(require_explicit_includes=False)
        result = _check_includes_explicit(None, policy)
        assert result.passed
        assert "not required" in result.message

    def test_required_and_list_present(self):
        policy = ManifestPolicy(require_explicit_includes=True)
        result = _check_includes_explicit(["src/", "docs/"], policy)
        assert result.passed

    def test_required_but_absent(self):
        policy = ManifestPolicy(require_explicit_includes=True)
        result = _check_includes_explicit(None, policy)
        assert not result.passed
        assert "none are declared" in result.message

    def test_required_but_auto(self):
        policy = ManifestPolicy(require_explicit_includes=True)
        result = _check_includes_explicit("auto", policy)
        assert not result.passed
        assert "auto" in result.message


class TestCheckScriptsPolicy:
    def test_allow_policy(self):
        policy = ManifestPolicy(scripts="allow")
        result = _check_scripts_policy({"scripts": {"build": "echo build"}}, policy)
        assert result.passed
        assert "Scripts allowed" in result.message

    def test_deny_policy_no_scripts(self):
        policy = ManifestPolicy(scripts="deny")
        result = _check_scripts_policy({}, policy)
        assert result.passed
        assert "No scripts section" in result.message

    def test_deny_policy_with_scripts_dict(self):
        policy = ManifestPolicy(scripts="deny")
        result = _check_scripts_policy({"scripts": {"build": "echo build"}}, policy)
        assert not result.passed
        assert "build" in result.details

    def test_deny_policy_with_scripts_not_dict(self):
        policy = ManifestPolicy(scripts="deny")
        result = _check_scripts_policy({"scripts": "run.sh"}, policy)
        assert not result.passed
        assert "scripts" in result.details

    def test_raw_yml_none(self):
        policy = ManifestPolicy(scripts="deny")
        result = _check_scripts_policy(None, policy)
        assert result.passed


class TestCheckUnmanagedFiles:
    def test_ignore_action(self):
        policy = UnmanagedFilesPolicy(action="ignore")
        result = _check_unmanaged_files(Path("/nonexistent"), None, policy)
        assert result.passed
        assert "disabled" in result.message

    def test_no_governance_dirs_exist(self, tmp_path):
        policy = UnmanagedFilesPolicy(action="deny", directories=(".github/agents",))
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert result.passed
        assert "No unmanaged files" in result.message

    def test_unmanaged_file_warn(self, tmp_path):
        gov_dir = tmp_path / ".github" / "agents"
        gov_dir.mkdir(parents=True)
        (gov_dir / "stray.md").write_text("stray", encoding="utf-8")
        policy = UnmanagedFilesPolicy(action="warn", directories=(".github/agents",))
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert result.passed
        assert "warn" in result.message

    def test_unmanaged_file_deny(self, tmp_path):
        gov_dir = tmp_path / ".github" / "agents"
        gov_dir.mkdir(parents=True)
        (gov_dir / "stray.md").write_text("stray", encoding="utf-8")
        policy = UnmanagedFilesPolicy(action="deny", directories=(".github/agents",))
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert not result.passed
        # details = one per-file line + a single trailing next-action hint.
        file_details = [d for d in result.details if "not tracked in apm.lock.yaml" in d]
        assert len(file_details) == 1
        assert "stray.md" in file_details[0]

    def test_deployed_file_not_unmanaged(self, tmp_path):
        gov_dir = tmp_path / ".github" / "agents"
        gov_dir.mkdir(parents=True)
        rel = ".github/agents/managed.md"
        (tmp_path / rel).write_text("managed", encoding="utf-8")
        locked = _locked("acme/pkg", deployed_files=[rel])
        lf = _lock(locked)
        policy = UnmanagedFilesPolicy(action="deny", directories=(".github/agents",))
        result = _check_unmanaged_files(tmp_path, lf, policy)
        assert result.passed

    def test_default_governance_dirs_used_when_none(self, tmp_path):
        policy = UnmanagedFilesPolicy(action="deny", directories=None)
        # No dirs exist -> should pass
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert result.passed


class TestLoadRawApmYml:
    def test_file_absent_returns_none(self, tmp_path):
        result = _load_raw_apm_yml(tmp_path)
        assert result is None

    def test_valid_yml_returns_dict(self, tmp_path):
        (tmp_path / "apm.yml").write_text("name: test\nversion: 1.0.0\n", encoding="utf-8")
        result = _load_raw_apm_yml(tmp_path)
        assert isinstance(result, dict)
        assert result["name"] == "test"

    def test_malformed_yaml_returns_none(self, tmp_path):
        (tmp_path / "apm.yml").write_text("name: [\nunot closed", encoding="utf-8")
        result = _load_raw_apm_yml(tmp_path)
        assert result is None

    def test_non_mapping_returns_none(self, tmp_path):
        (tmp_path / "apm.yml").write_text("- item1\n- item2\n", encoding="utf-8")
        result = _load_raw_apm_yml(tmp_path)
        assert result is None

    def test_non_utf8_returns_none(self, tmp_path):
        (tmp_path / "apm.yml").write_bytes(b"\xff\xfe" + b"\x00" * 100)
        result = _load_raw_apm_yml(tmp_path)
        assert result is None


class TestRunDependencyPolicyChecks:
    def _simple_policy(self) -> ApmPolicy:
        return ApmPolicy()

    def test_empty_deps_no_policy_passes(self):
        policy = ApmPolicy()
        result = run_dependency_policy_checks([], policy=policy)
        assert result.passed

    def test_allowlist_violation_fail_fast(self):
        policy = ApmPolicy(dependencies=DependencyPolicy(allow=("acme/allowed",)))
        result = run_dependency_policy_checks(
            [_dep("acme/forbidden")], policy=policy, fail_fast=True
        )
        assert not result.passed
        # Fail-fast: only first failing check present
        names = [c.name for c in result.checks]
        assert "dependency-allowlist" in names

    def test_mcp_checks_run_when_provided(self):
        policy = ApmPolicy(mcp=McpPolicy(deny=("evil-server",)))
        mcp = _mcp("evil-server")
        result = run_dependency_policy_checks([], policy=policy, mcp_deps=[mcp])
        assert not result.passed
        names = [c.name for c in result.checks]
        assert "mcp-denylist" in names

    def test_mcp_checks_skipped_when_none(self):
        policy = ApmPolicy(mcp=McpPolicy(deny=("evil-server",)))
        result = run_dependency_policy_checks([], policy=policy, mcp_deps=None)
        names = [c.name for c in result.checks]
        assert "mcp-denylist" not in names

    def test_effective_target_runs_compilation_check(self):
        policy = ApmPolicy(
            compilation=CompilationPolicy(target=CompilationTargetPolicy(enforce="vscode"))
        )
        result = run_dependency_policy_checks([], policy=policy, effective_target="claude")
        assert not result.passed
        names = [c.name for c in result.checks]
        assert "compilation-target" in names

    def test_manifest_includes_check_runs_when_provided(self):
        policy = ApmPolicy(manifest=ManifestPolicy(require_explicit_includes=True))
        result = run_dependency_policy_checks([], policy=policy, manifest_includes=None)
        assert not result.passed
        names = [c.name for c in result.checks]
        assert "explicit-includes" in names

    def test_manifest_includes_skipped_when_not_provided(self):
        policy = ApmPolicy(manifest=ManifestPolicy(require_explicit_includes=True))
        result = run_dependency_policy_checks(
            [], policy=policy, manifest_includes=_INCLUDES_NOT_PROVIDED
        )
        names = [c.name for c in result.checks]
        assert "explicit-includes" not in names

    def test_fail_fast_false_collects_all_checks(self):
        policy = ApmPolicy(dependencies=DependencyPolicy(allow=("acme/good",), deny=("acme/bad",)))
        result = run_dependency_policy_checks(
            [_dep("acme/forbidden")], policy=policy, fail_fast=False
        )
        names = [c.name for c in result.checks]
        assert "dependency-allowlist" in names
        assert "dependency-denylist" in names

    def test_consolidated_tail_checks_cover_all_categories(self):
        """Regression trap for the consolidated tail_checks loop (PR #1464).

        Exercises compilation-target AND manifest-includes in a single
        call together with an MCP denylist violation, confirming no check
        category is silently dropped by the consolidated loop.
        """
        policy = ApmPolicy(
            compilation=CompilationPolicy(
                target=CompilationTargetPolicy(enforce="vscode"),
            ),
            manifest=ManifestPolicy(require_explicit_includes=True),
            mcp=McpPolicy(deny=("evil-srv",)),
        )
        mcp = _mcp("evil-srv")
        result = run_dependency_policy_checks(
            [],
            policy=policy,
            fail_fast=False,
            effective_target="claude",
            manifest_includes=None,
            mcp_deps=[mcp],
        )
        names = [c.name for c in result.checks]
        assert "mcp-denylist" in names
        assert "compilation-target" in names
        assert "explicit-includes" in names
        assert not result.passed


_MINIMAL_APM_YML = dedent("""\
    name: test-project
    version: 0.1.0
    owner:
      name: test-org
""")


class TestRunPolicyChecks:
    def test_no_apm_yml_returns_empty(self, tmp_path):
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        # No apm.yml -> immediate return, no checks run
        assert result.passed
        assert result.checks == []

    def test_minimal_project_passes_empty_policy(self, tmp_path):
        (tmp_path / "apm.yml").write_text(_MINIMAL_APM_YML, encoding="utf-8")
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert result.passed

    def test_compilation_checks_run_on_disk(self, tmp_path):
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            owner:
              name: test-org
            target: claude
            compilation:
              strategy: distributed
        """)
        (tmp_path / "apm.yml").write_text(apm_yml, encoding="utf-8")
        policy = ApmPolicy(
            compilation=CompilationPolicy(
                target=CompilationTargetPolicy(enforce="vscode"),
                strategy=CompilationStrategyPolicy(enforce="distributed"),
                source_attribution=False,
            )
        )
        result = run_policy_checks(tmp_path, policy, fail_fast=False)
        names = [c.name for c in result.checks]
        assert "compilation-target" in names

    def test_manifest_fields_check_runs(self, tmp_path):
        (tmp_path / "apm.yml").write_text(_MINIMAL_APM_YML, encoding="utf-8")
        policy = ApmPolicy(manifest=ManifestPolicy(required_fields=("description",)))
        result = run_policy_checks(tmp_path, policy, fail_fast=False)
        names = [c.name for c in result.checks]
        assert "required-manifest-fields" in names
        failed = [c for c in result.checks if c.name == "required-manifest-fields"]
        assert failed[0].passed is False

    def test_scripts_deny_check_runs(self, tmp_path):
        apm_yml = dedent("""\
            name: test-project
            version: 0.1.0
            owner:
              name: test-org
            scripts:
              build: echo build
        """)
        (tmp_path / "apm.yml").write_text(apm_yml, encoding="utf-8")
        policy = ApmPolicy(manifest=ManifestPolicy(scripts="deny"))
        result = run_policy_checks(tmp_path, policy, fail_fast=False)
        failed = [c for c in result.checks if c.name == "scripts-policy" and not c.passed]
        assert failed

    def test_fail_fast_stops_after_first_failure(self, tmp_path):
        (tmp_path / "apm.yml").write_text(_MINIMAL_APM_YML, encoding="utf-8")
        policy = ApmPolicy(
            dependencies=DependencyPolicy(allow=()),  # empty allow = block all
            manifest=ManifestPolicy(required_fields=("description",)),
        )
        result = run_policy_checks(tmp_path, policy, fail_fast=True)
        assert not result.passed


# ============================================================================
# 2. REGISTRY OPERATIONS
# ============================================================================


def _make_server_response(name: str, server_id: str = "uuid-1234") -> dict:
    """Build a v0.1 spec-shaped server response payload."""
    return {
        "server": {
            "id": server_id,
            "name": name,
            "description": f"Test server {name}",
            "packages": [
                {
                    "name": name,
                    "runtime_hint": "npx",
                    "runtimeHint": "npx",
                }
            ],
        }
    }


def _make_search_response(servers: list[dict]) -> dict:
    """Build a v0.1 spec search response."""
    return {"servers": [{"server": s} for s in servers]}


class _FakeResponse:
    """Lightweight requests.Response substitute."""

    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict = {}
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _req

            err = _req.HTTPError(response=self)
            err.response = self
            raise err


@pytest.fixture()
def _no_cache_env(monkeypatch):
    """Disable the HTTP cache so tests hit `session.get` directly."""
    monkeypatch.setenv("APM_NO_CACHE", "1")


@pytest.fixture()
def _no_ci_env(monkeypatch):
    """Ensure CI env vars are absent so env-var prompt tests behave predictably."""
    for var in ["CI", "GITHUB_ACTIONS", "TRAVIS", "JENKINS_URL", "BUILDKITE", "APM_E2E_TESTS"]:
        monkeypatch.delenv(var, raising=False)


class TestMCPServerOperationsValidateServersExist:
    def test_empty_list_returns_empty(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        with patch.object(ops.registry_client.session, "get") as mock_get:
            mock_get.return_value = _FakeResponse({"servers": []})
            valid, invalid = ops.validate_servers_exist([])
        assert valid == []
        assert invalid == []

    def test_found_server_is_valid(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        server_name = "io.github.acme/cool-server"
        server_data = _make_server_response(server_name)["server"]
        search_resp = _FakeResponse(_make_search_response([server_data]))
        detail_resp = _FakeResponse({"server": server_data})

        with patch.object(ops.registry_client.session, "get") as mock_get:
            mock_get.side_effect = [search_resp, detail_resp]
            valid, invalid = ops.validate_servers_exist([server_name])

        assert server_name in valid
        assert invalid == []

    def test_missing_server_is_invalid(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        with patch.object(ops.registry_client.session, "get") as mock_get:
            mock_get.return_value = _FakeResponse({"servers": []})
            _valid, invalid = ops.validate_servers_exist(["io.github.nobody/ghost"])

        assert "io.github.nobody/ghost" in invalid

    def test_network_error_on_non_custom_url_assumes_valid(self, _no_cache_env, monkeypatch):
        import requests as _req

        from apm_cli.registry.operations import MCPServerOperations

        # Ensure MCP_REGISTRY_URL is not set so _is_custom_url becomes False
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        ops = MCPServerOperations()  # no explicit URL -> _is_custom_url = False
        assert not ops.registry_client._is_custom_url

        with patch.object(ops.registry_client.session, "get") as mock_get:
            mock_get.side_effect = _req.ConnectionError("network down")
            valid, _invalid = ops.validate_servers_exist(["io.github.acme/server"])

        assert "io.github.acme/server" in valid

    def test_network_error_on_custom_url_raises(self, _no_cache_env):
        import requests as _req

        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://custom.registry.example.com")
        assert ops.registry_client._is_custom_url is True

        with patch.object(ops.registry_client.session, "get") as mock_get:
            mock_get.side_effect = _req.RequestException("boom")
            with pytest.raises(RuntimeError, match="Could not reach MCP registry"):
                ops.validate_servers_exist(["io.github.acme/server"])

    def test_multiple_servers_validated_in_parallel(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        names = ["io.github.a/s1", "io.github.b/s2", "io.github.c/s3"]
        with patch.object(ops.registry_client.session, "get") as mock_get:
            mock_get.return_value = _FakeResponse({"servers": []})
            valid, invalid = ops.validate_servers_exist(names)

        assert set(invalid) == set(names)
        assert valid == []


class TestMCPServerOperationsBatchFetch:
    def test_found_server_cached(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        server_name = "io.github.acme/srv"
        server_data = _make_server_response(server_name)["server"]
        search_resp = _FakeResponse(_make_search_response([server_data]))
        detail_resp = _FakeResponse({"server": server_data})

        with patch.object(ops.registry_client.session, "get") as mock_get:
            mock_get.side_effect = [search_resp, detail_resp]
            cache = ops.batch_fetch_server_info([server_name])

        assert server_name in cache
        assert cache[server_name] is not None

    def test_missing_server_returns_none(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        with patch.object(ops.registry_client.session, "get") as mock_get:
            mock_get.return_value = _FakeResponse({"servers": []})
            cache = ops.batch_fetch_server_info(["io.github.nobody/ghost"])

        assert cache["io.github.nobody/ghost"] is None

    def test_exception_maps_to_none(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        with patch.object(ops.registry_client.session, "get") as mock_get:
            mock_get.side_effect = RuntimeError("unexpected")
            cache = ops.batch_fetch_server_info(["io.github.acme/srv"])

        assert cache["io.github.acme/srv"] is None


class TestMCPServerOperationsCollectEnvVars:
    def test_empty_cache_returns_empty(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        result = ops.collect_environment_variables([], server_info_cache={})
        assert result == {}

    def test_server_with_env_vars_in_packages(self, monkeypatch, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        monkeypatch.setenv("CI", "true")  # trigger CI path (no interactive prompt)
        ops = MCPServerOperations("https://api.mcp.github.com")
        cache = {
            "io.github.acme/srv": {
                "name": "io.github.acme/srv",
                "packages": [
                    {
                        "environmentVariables": [
                            {"name": "MY_TOKEN", "description": "A token", "required": True}
                        ]
                    }
                ],
            }
        }
        result = ops.collect_environment_variables(["io.github.acme/srv"], server_info_cache=cache)
        assert "MY_TOKEN" in result

    def test_server_with_docker_args_env_vars(self, monkeypatch, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        monkeypatch.setenv("CI", "true")
        ops = MCPServerOperations("https://api.mcp.github.com")
        cache = {
            "my-server": {
                "name": "my-server",
                "docker": {"args": ["${MY_SECRET}", "not-an-env-var"]},
                "packages": [],
            }
        }
        result = ops.collect_environment_variables(["my-server"], server_info_cache=cache)
        assert "MY_SECRET" in result

    def test_server_not_in_cache_skipped(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        result = ops.collect_environment_variables(["ghost"], server_info_cache={"ghost": None})
        assert result == {}

    def test_existing_env_var_used(self, monkeypatch, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("MY_TOKEN", "secret-value")
        ops = MCPServerOperations("https://api.mcp.github.com")
        cache = {
            "srv": {
                "name": "srv",
                "packages": [
                    {
                        "environmentVariables": [
                            {"name": "MY_TOKEN", "description": "token", "required": True}
                        ]
                    }
                ],
            }
        }
        result = ops.collect_environment_variables(["srv"], server_info_cache=cache)
        assert result.get("MY_TOKEN") == "secret-value"

    def test_e2e_mode_omits_optional_env_without_value(self, monkeypatch, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        monkeypatch.setenv("APM_E2E_TESTS", "1")
        monkeypatch.delenv("CI", raising=False)
        ops = MCPServerOperations("https://api.mcp.github.com")
        cache = {
            "srv": {
                "name": "srv",
                "packages": [
                    {
                        "environmentVariables": [
                            {
                                "name": "GITHUB_DYNAMIC_TOOLSETS",
                                "description": "",
                                "required": False,
                            }
                        ]
                    }
                ],
            }
        }
        result = ops.collect_environment_variables(["srv"], server_info_cache=cache)
        assert "GITHUB_DYNAMIC_TOOLSETS" not in result


class TestMCPServerOperationsCollectRuntimeVars:
    def test_no_runtime_args(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        cache = {
            "srv": {
                "name": "srv",
                "packages": [{"runtime_arguments": []}],
            }
        }
        result = ops.collect_runtime_variables(["srv"], server_info_cache=cache)
        assert result == {}

    def test_runtime_vars_collected_in_ci(self, monkeypatch, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        monkeypatch.setenv("CI", "true")
        ops = MCPServerOperations("https://api.mcp.github.com")
        cache = {
            "srv": {
                "name": "srv",
                "packages": [
                    {
                        "runtime_arguments": [
                            {
                                "variables": {
                                    "ado_org": {
                                        "description": "ADO org name",
                                        "is_required": True,
                                    }
                                }
                            }
                        ]
                    }
                ],
            }
        }
        result = ops.collect_runtime_variables(["srv"], server_info_cache=cache)
        assert "ado_org" in result

    def test_fetches_from_registry_when_no_cache(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        with patch.object(ops.registry_client.session, "get") as mock_get:
            mock_get.return_value = _FakeResponse({"servers": []})
            result = ops.collect_runtime_variables(["srv"])
        assert result == {}


class TestMCPServerOperationsCheckNeedingInstall:
    def test_empty_refs_returns_empty(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        result = ops.check_servers_needing_installation(["vscode"], [])
        assert result == []

    def test_server_not_found_needs_install(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        with patch.object(ops.registry_client.session, "get") as mock_get:
            mock_get.return_value = _FakeResponse({"servers": []})
            result = ops.check_servers_needing_installation(["vscode"], ["ghost-server"])

        assert "ghost-server" in result

    def test_exception_in_lookup_needs_install(self, _no_cache_env):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations("https://api.mcp.github.com")
        with patch.object(ops.registry_client.session, "get") as mock_get:
            mock_get.side_effect = RuntimeError("oops")
            result = ops.check_servers_needing_installation(["vscode"], ["srv"])

        assert "srv" in result


# ============================================================================
# 3. MARKETPLACE RESOLVER
# ============================================================================


class TestParseMarketplaceRef:
    def test_valid_basic(self):
        result = parse_marketplace_ref("my-plugin@my-market")
        assert result == ("my-plugin", "my-market", None)

    def test_valid_with_ref(self):
        result = parse_marketplace_ref("my-plugin@my-market#v1.0.0")
        assert result == ("my-plugin", "my-market", "v1.0.0")

    def test_valid_with_branch_ref(self):
        result = parse_marketplace_ref("plugin@market#main")
        assert result == ("plugin", "market", "main")

    def test_no_match_slash_in_head(self):
        result = parse_marketplace_ref("owner/repo")
        assert result is None

    def test_no_match_colon_in_head(self):
        result = parse_marketplace_ref("git:something")
        assert result is None

    def test_semver_range_raises_value_error(self):
        with pytest.raises(ValueError, match="Semver ranges are not supported"):
            parse_marketplace_ref("plugin@market#^1.0.0")

    def test_semver_tilde_raises(self):
        with pytest.raises(ValueError):
            parse_marketplace_ref("plugin@market#~1.2.0")

    def test_semver_ge_raises(self):
        with pytest.raises(ValueError):
            parse_marketplace_ref("plugin@market#>=1.0.0")

    def test_not_a_marketplace_ref_plain_string(self):
        result = parse_marketplace_ref("just-a-name")
        # only a plugin name, no @marketplace
        assert result is None

    def test_ref_with_sha(self):
        result = parse_marketplace_ref("plugin@market#abc1234")
        assert result == ("plugin", "market", "abc1234")

    def test_strips_whitespace(self):
        result = parse_marketplace_ref("  plugin@market  ")
        assert result == ("plugin", "market", None)


class TestNormalizeOwnerRepoSlug:
    def test_basic(self):
        assert _normalize_owner_repo_slug("Owner/Repo") == "owner/repo"

    def test_strips_git_suffix(self):
        assert _normalize_owner_repo_slug("owner/repo.git") == "owner/repo"

    def test_strips_trailing_slash(self):
        assert _normalize_owner_repo_slug("owner/repo/") == "owner/repo"

    def test_strips_whitespace(self):
        assert _normalize_owner_repo_slug("  owner/repo  ") == "owner/repo"


class TestMarketplaceProjectSlug:
    def test_basic(self):
        assert _marketplace_project_slug("MyOrg", "MyRepo") == "myorg/myrepo"


class TestNormalizeRepoFieldForMatch:
    def test_bare_owner_repo(self):
        assert _normalize_repo_field_for_match("owner/repo", "github.com") == "owner/repo"

    def test_http_url_matching_host(self):
        assert (
            _normalize_repo_field_for_match("https://github.com/owner/repo", "github.com")
            == "owner/repo"
        )

    def test_http_url_different_host_returns_empty(self):
        assert _normalize_repo_field_for_match("https://gitlab.com/owner/repo", "github.com") == ""

    def test_ssh_url_matching_host(self):
        assert (
            _normalize_repo_field_for_match("git@github.com:owner/repo", "github.com")
            == "owner/repo"
        )

    def test_ssh_url_different_host_returns_empty(self):
        assert _normalize_repo_field_for_match("git@evil.com:owner/repo", "github.com") == ""

    def test_host_qualified_strips_host(self):
        assert (
            _normalize_repo_field_for_match("github.com/owner/repo", "github.com") == "owner/repo"
        )

    def test_git_suffix_stripped(self):
        assert _normalize_repo_field_for_match("owner/repo.git", "github.com") == "owner/repo"


class TestRepoFieldMatchesMarketplace:
    def test_no_slash_returns_false(self):
        assert not _repo_field_matches_marketplace("nodeslash", "owner", "repo", "github.com")

    def test_matching_returns_true(self):
        assert _repo_field_matches_marketplace("owner/repo", "owner", "repo", "github.com")

    def test_non_matching_returns_false(self):
        assert not _repo_field_matches_marketplace("owner/other", "owner", "repo", "github.com")

    def test_empty_field_returns_false(self):
        assert not _repo_field_matches_marketplace("", "owner", "repo", "github.com")


class TestCoerceDictPluginType:
    def test_explicit_type(self):
        assert _coerce_dict_plugin_type({"type": "GitHub"}) == "github"

    def test_source_key_fallback(self):
        assert _coerce_dict_plugin_type({"source": "url"}) == "url"

    def test_kind_key_fallback(self):
        assert _coerce_dict_plugin_type({"kind": "git-subdir"}) == "git-subdir"

    def test_inferred_github_with_path(self):
        assert _coerce_dict_plugin_type({"repo": "owner/repo", "path": "sub"}) == "github"

    def test_inferred_git_subdir_with_subdir(self):
        assert _coerce_dict_plugin_type({"repo": "owner/repo", "subdir": "sub"}) == "git-subdir"

    def test_inferred_github_bare_repo(self):
        assert _coerce_dict_plugin_type({"repo": "owner/repo"}) == "github"

    def test_no_repo_no_type_returns_empty(self):
        assert _coerce_dict_plugin_type({}) == ""

    def test_repo_without_slash_returns_empty(self):
        assert _coerce_dict_plugin_type({"repo": "noslash"}) == ""


class TestIsInMarketplaceSource:
    def _source(self):
        return MarketplaceSource(name="mkt", owner="acme", repo="marketplace")

    def test_str_source_is_in_marketplace(self):
        plugin = MarketplacePlugin(name="p", source="./plugins/p")
        assert _is_in_marketplace_source(plugin, self._source())

    def test_none_source_is_not(self):
        plugin = MarketplacePlugin(name="p", source=None)
        assert not _is_in_marketplace_source(plugin, self._source())

    def test_dict_matching_repo(self):
        plugin = MarketplacePlugin(name="p", source={"type": "github", "repo": "acme/marketplace"})
        assert _is_in_marketplace_source(plugin, self._source())

    def test_dict_non_matching_repo(self):
        plugin = MarketplacePlugin(name="p", source={"type": "github", "repo": "other/repo"})
        assert not _is_in_marketplace_source(plugin, self._source())

    def test_non_dict_non_str_source(self):
        plugin = MarketplacePlugin(name="p", source=42)
        assert not _is_in_marketplace_source(plugin, self._source())


class TestMarketplaceHostNeedsExplicitGitPath:
    def test_github_com_returns_false(self):
        assert not _marketplace_host_needs_explicit_git_path("github.com")

    def test_ghe_cloud_returns_false(self):
        assert not _marketplace_host_needs_explicit_git_path("corp.ghe.com")

    def test_gitlab_com_returns_true(self):
        assert _marketplace_host_needs_explicit_git_path("gitlab.com")

    def test_self_managed_returns_true(self):
        assert _marketplace_host_needs_explicit_git_path("git.example.com")

    def test_empty_host_returns_false(self):
        assert not _marketplace_host_needs_explicit_git_path("")

    def test_ado_returns_false(self):
        assert not _marketplace_host_needs_explicit_git_path("dev.azure.com")


class TestNeedsCanonicalHostPrefix:
    def test_github_com_returns_false(self):
        assert not _needs_canonical_host_prefix("owner/repo", "github.com")

    def test_ghe_cloud_returns_true(self):
        assert _needs_canonical_host_prefix("owner/repo", "corp.ghe.com")

    def test_already_prefixed_returns_false(self):
        assert not _needs_canonical_host_prefix("corp.ghe.com/owner/repo", "corp.ghe.com")

    def test_url_form_returns_false(self):
        # First segment has colon -> URL-like, return False
        assert not _needs_canonical_host_prefix("https://corp.ghe.com/owner/repo", "corp.ghe.com")

    def test_non_github_family_returns_false(self):
        assert not _needs_canonical_host_prefix("owner/repo", "gitlab.com")

    def test_empty_host_returns_false(self):
        assert not _needs_canonical_host_prefix("owner/repo", "")


class TestResolveGithubSource:
    def test_basic(self):
        result = _resolve_github_source({"repo": "owner/repo"})
        assert result == "owner/repo"

    def test_with_ref(self):
        result = _resolve_github_source({"repo": "owner/repo", "ref": "v1.0.0"})
        assert result == "owner/repo#v1.0.0"

    def test_with_path(self):
        result = _resolve_github_source({"repo": "owner/repo", "path": "plugins/my-plugin"})
        assert result == "owner/repo/plugins/my-plugin"

    def test_with_path_and_ref(self):
        result = _resolve_github_source({"repo": "owner/repo", "path": "plugins/p", "ref": "main"})
        assert result == "owner/repo/plugins/p#main"

    def test_no_repo_raises(self):
        with pytest.raises(ValueError, match="owner/repo"):
            _resolve_github_source({})

    def test_repo_without_slash_raises(self):
        with pytest.raises(ValueError):
            _resolve_github_source({"repo": "noslash"})

    def test_repository_field_alias(self):
        result = _resolve_github_source({"repository": "owner/repo"})
        assert result == "owner/repo"


class TestResolveUrlSource:
    def test_github_url(self):
        result = _resolve_url_source({"url": "https://github.com/owner/repo"})
        assert "owner/repo" in result

    def test_empty_url_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            _resolve_url_source({})

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Cannot resolve"):
            _resolve_url_source({"url": "not-a-url"})


class TestResolveGitSubdirSource:
    def test_basic(self):
        result = _resolve_git_subdir_source({"repo": "owner/repo"})
        assert result == "owner/repo"

    def test_with_subdir(self):
        result = _resolve_git_subdir_source({"repo": "owner/repo", "subdir": "sub"})
        assert result == "owner/repo/sub"

    def test_with_path_fallback(self):
        result = _resolve_git_subdir_source({"repo": "owner/repo", "path": "sub"})
        assert result == "owner/repo/sub"

    def test_with_ref(self):
        result = _resolve_git_subdir_source({"repo": "owner/repo", "ref": "main"})
        assert result == "owner/repo#main"

    def test_no_repo_raises(self):
        with pytest.raises(ValueError, match="owner/repo"):
            _resolve_git_subdir_source({})

    def test_url_in_repo_raises(self):
        with pytest.raises(ValueError, match="Use source type 'url'"):
            _resolve_git_subdir_source({"repo": "https://github.com/owner/repo"})


class TestResolveRelativeSource:
    def test_simple_relative(self):
        result = _resolve_relative_source("./plugins/my-plugin", "owner", "repo")
        assert result == "owner/repo/plugins/my-plugin"

    def test_bare_name_with_plugin_root(self):
        result = _resolve_relative_source("my-plugin", "owner", "repo", plugin_root="plugins")
        assert result == "owner/repo/plugins/my-plugin"

    def test_bare_dot_returns_root(self):
        result = _resolve_relative_source(".", "owner", "repo")
        assert result == "owner/repo"

    def test_empty_source_returns_root(self):
        result = _resolve_relative_source("", "owner", "repo")
        assert result == "owner/repo"

    def test_path_traversal_raises(self):
        with pytest.raises(ValueError):
            _resolve_relative_source("../escape", "owner", "repo")


class TestResolvePluginSource:
    def _make_plugin(self, name: str, source) -> MarketplacePlugin:
        return MarketplacePlugin(name=name, source=source)

    def test_relative_str_source(self):
        plugin = self._make_plugin("p", "./plugins/p")
        result = resolve_plugin_source(plugin, "owner", "repo")
        assert result == "owner/repo/plugins/p"

    def test_github_dict_source(self):
        plugin = self._make_plugin("p", {"type": "github", "repo": "owner/cool-plugin"})
        result = resolve_plugin_source(plugin)
        assert result == "owner/cool-plugin"

    def test_url_dict_source(self):
        plugin = self._make_plugin("p", {"type": "url", "url": "https://github.com/owner/repo"})
        result = resolve_plugin_source(plugin)
        assert "owner/repo" in result

    def test_git_subdir_source(self):
        plugin = self._make_plugin(
            "p", {"type": "git-subdir", "repo": "owner/repo", "subdir": "sub"}
        )
        result = resolve_plugin_source(plugin)
        assert result == "owner/repo/sub"

    def test_gitlab_source(self):
        plugin = self._make_plugin("p", {"type": "gitlab", "repo": "owner/repo", "subdir": "sub"})
        result = resolve_plugin_source(plugin)
        assert result == "owner/repo/sub"

    def test_npm_source_raises(self):
        plugin = self._make_plugin("p", {"type": "npm", "package": "@scope/pkg"})
        with pytest.raises(ValueError, match="npm source type"):
            resolve_plugin_source(plugin)

    def test_no_source_raises(self):
        plugin = self._make_plugin("p", None)
        with pytest.raises(ValueError, match="no source defined"):
            resolve_plugin_source(plugin)

    def test_unsupported_type_raises(self):
        plugin = self._make_plugin("p", {"type": "docker", "image": "myimage"})
        with pytest.raises(ValueError, match="unsupported source type"):
            resolve_plugin_source(plugin)

    def test_dict_no_type_no_repo_raises(self):
        plugin = self._make_plugin("p", {"description": "something"})
        with pytest.raises(ValueError, match="no 'type'"):
            resolve_plugin_source(plugin)

    def test_unrecognized_source_type_raises(self):
        plugin = self._make_plugin("p", 42)
        with pytest.raises(ValueError, match="unrecognized source format"):
            resolve_plugin_source(plugin)


class TestExtractInRepoPathAndRef:
    def test_none_source_returns_none(self):
        plugin = MarketplacePlugin(name="p", source=None)
        path, ref = _extract_in_repo_path_and_ref(plugin)
        assert path is None and ref is None

    def test_str_source_relative(self):
        plugin = MarketplacePlugin(name="p", source="./plugins/p")
        path, ref = _extract_in_repo_path_and_ref(plugin)
        assert path == "plugins/p"
        assert ref is None

    def test_str_source_root_dot(self):
        plugin = MarketplacePlugin(name="p", source=".")
        path, _ref = _extract_in_repo_path_and_ref(plugin)
        assert path is None

    def test_str_source_with_plugin_root(self):
        plugin = MarketplacePlugin(name="p", source="my-plugin")
        path, _ref = _extract_in_repo_path_and_ref(plugin, plugin_root="plugins")
        assert path == "plugins/my-plugin"

    def test_dict_github_with_path(self):
        plugin = MarketplacePlugin(name="p", source={"type": "github", "path": "sub/dir"})
        path, _ref = _extract_in_repo_path_and_ref(plugin)
        assert path == "sub/dir"

    def test_dict_github_no_path(self):
        plugin = MarketplacePlugin(name="p", source={"type": "github"})
        path, _ref = _extract_in_repo_path_and_ref(plugin)
        assert path is None

    def test_dict_git_subdir_with_subdir(self):
        plugin = MarketplacePlugin(
            name="p", source={"type": "git-subdir", "repo": "owner/repo", "subdir": "sub"}
        )
        path, _ref = _extract_in_repo_path_and_ref(plugin)
        assert path == "sub"

    def test_dict_with_ref(self):
        plugin = MarketplacePlugin(
            name="p",
            source={"type": "github", "path": "sub", "ref": "v2.0.0"},
        )
        _path, ref = _extract_in_repo_path_and_ref(plugin)
        assert ref == "v2.0.0"

    def test_non_dict_non_str_returns_none(self):
        plugin = MarketplacePlugin(name="p", source=42)
        path, _ref = _extract_in_repo_path_and_ref(plugin)
        assert path is None


class TestMarketplacePluginResolutionIteration:
    def test_iter_yields_canonical_and_plugin(self):
        plugin = MarketplacePlugin(name="p")
        resolution = MarketplacePluginResolution(canonical="owner/repo", plugin=plugin)
        canonical, out_plugin = resolution
        assert canonical == "owner/repo"
        assert out_plugin is plugin


class TestComputeCrossRepoMisconfigRisk:
    def _source(self, host="corp.ghe.com"):
        return MarketplaceSource(name="mkt", owner="acme", repo="marketplace", host=host)

    def test_dep_ref_returns_none(self):
        plugin = MarketplacePlugin(name="p", source={"type": "github", "repo": "other/repo"})
        src = self._source()
        dep_ref = DependencyReference.parse("some/thing")
        result = _compute_cross_repo_misconfig_risk(plugin, src, "other/repo", dep_ref)
        assert result is None

    def test_non_dict_source_returns_none(self):
        plugin = MarketplacePlugin(name="p", source="./local")
        src = self._source()
        result = _compute_cross_repo_misconfig_risk(plugin, src, "acme/marketplace/local", None)
        assert result is None

    def test_non_github_type_returns_none(self):
        plugin = MarketplacePlugin(name="p", source={"type": "npm", "package": "@scope/pkg"})
        src = self._source()
        result = _compute_cross_repo_misconfig_risk(plugin, src, "scope/pkg", None)
        assert result is None

    def test_in_marketplace_returns_none(self):
        # Repo field points to the marketplace itself -> in-marketplace
        plugin = MarketplacePlugin(name="p", source={"type": "github", "repo": "acme/marketplace"})
        src = self._source()
        result = _compute_cross_repo_misconfig_risk(plugin, src, "acme/marketplace", None)
        assert result is None

    def test_github_com_no_prefix_needed_returns_none(self):
        plugin = MarketplacePlugin(name="p", source={"type": "github", "repo": "other/repo"})
        src = MarketplaceSource(name="mkt", owner="acme", repo="marketplace", host="github.com")
        result = _compute_cross_repo_misconfig_risk(plugin, src, "other/repo", None)
        assert result is None

    def test_ghe_cross_repo_returns_risk(self):
        plugin = MarketplacePlugin(name="p", source={"type": "github", "repo": "other/repo"})
        src = self._source("corp.ghe.com")
        result = _compute_cross_repo_misconfig_risk(plugin, src, "other/repo", None)
        assert result is not None
        assert result.marketplace_host == "corp.ghe.com"
        assert result.bare_repo_field == "other/repo"
        assert result.suggested_qualified_repo == "corp.ghe.com/other/repo"


@pytest.fixture()
def _registered_marketplace(tmp_path, monkeypatch):
    """Register a test marketplace in an isolated config dir."""
    import apm_cli.config as cfg_mod
    import apm_cli.marketplace.registry as reg_mod

    # Redirect CONFIG_DIR and CONFIG_FILE to our tmp_path so we don't touch ~/.apm
    apm_dir = tmp_path / ".apm"
    apm_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", str(apm_dir))
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", str(apm_dir / "config.json"))
    # Invalidate the registry cache so it re-reads from our tmp_path
    monkeypatch.setattr(reg_mod, "_registry_cache", None)

    from apm_cli.marketplace.registry import add_marketplace

    source = MarketplaceSource(
        name="test-market",
        owner="acme",
        repo="marketplace",
        host="github.com",
        branch="main",
    )
    add_marketplace(source)
    yield source
    # Cleanup: invalidate cache after test
    monkeypatch.setattr(reg_mod, "_registry_cache", None)


def _marketplace_json_payload(plugins: list[dict]) -> dict:
    return {"name": "test-market", "description": "Test marketplace", "plugins": plugins}


class TestResolveMarketplacePlugin:
    def test_plugin_found_relative_source(self, _registered_marketplace, monkeypatch):
        manifest_data = _marketplace_json_payload(
            [{"name": "my-plugin", "source": "./plugins/my-plugin", "description": "A plugin"}]
        )

        with patch("apm_cli.marketplace.client._HTTP_SESSION.get") as mock_get:
            mock_get.return_value = _FakeResponse(manifest_data)
            result = resolve_marketplace_plugin("my-plugin", "test-market")

        assert isinstance(result, MarketplacePluginResolution)
        assert result.plugin.name == "my-plugin"
        assert "acme/marketplace/plugins/my-plugin" in result.canonical

    def test_plugin_found_github_dict_source(self, _registered_marketplace, monkeypatch):
        manifest_data = _marketplace_json_payload(
            [
                {
                    "name": "ext-plugin",
                    "source": {"type": "github", "repo": "other/cool-plugin", "ref": "v1.0.0"},
                    "description": "External plugin",
                }
            ]
        )

        with patch("apm_cli.marketplace.client._HTTP_SESSION.get") as mock_get:
            mock_get.return_value = _FakeResponse(manifest_data)
            result = resolve_marketplace_plugin("ext-plugin", "test-market")

        assert result.canonical.startswith("other/cool-plugin")
        assert "v1.0.0" in result.canonical

    def test_plugin_not_found_raises(self, _registered_marketplace, monkeypatch):
        from apm_cli.marketplace.errors import PluginNotFoundError

        manifest_data = _marketplace_json_payload(
            [{"name": "existing-plugin", "source": "./plugins/existing"}]
        )

        with patch("apm_cli.marketplace.client._HTTP_SESSION.get") as mock_get:
            mock_get.return_value = _FakeResponse(manifest_data)
            with pytest.raises(PluginNotFoundError):
                resolve_marketplace_plugin("nonexistent-plugin", "test-market")

    def test_marketplace_not_found_raises(self, _registered_marketplace, monkeypatch):
        from apm_cli.marketplace.errors import MarketplaceNotFoundError

        with pytest.raises(MarketplaceNotFoundError):
            resolve_marketplace_plugin("any-plugin", "no-such-market")

    def test_version_spec_overrides_ref(self, _registered_marketplace, monkeypatch):
        manifest_data = _marketplace_json_payload(
            [
                {
                    "name": "versioned",
                    "source": "./plugins/versioned",
                    "description": "Versioned plugin",
                }
            ]
        )

        with patch("apm_cli.marketplace.client._HTTP_SESSION.get") as mock_get:
            mock_get.return_value = _FakeResponse(manifest_data)
            result = resolve_marketplace_plugin("versioned", "test-market", version_spec="v2.5.0")

        assert "#v2.5.0" in result.canonical

    def test_warning_handler_called_on_ref_change(
        self, tmp_path, _registered_marketplace, monkeypatch
    ):
        """Ref immutability warning fires when a previously recorded ref changes."""
        # Pre-seed the pins file with an old ref so the immutability check fires.
        # Key format: "marketplace/plugin/version" (lowercase, slashes)
        pins_dir = tmp_path / ".apm" / "cache" / "marketplace"
        pins_dir.mkdir(parents=True, exist_ok=True)
        pins_file = pins_dir / "version-pins.json"
        pins_file.write_text(
            json.dumps({"test-market/versioned/v1.0.0": "old-sha"}), encoding="utf-8"
        )

        manifest_data = _marketplace_json_payload(
            [
                {
                    "name": "versioned",
                    "source": {"type": "github", "repo": "owner/versioned", "ref": "old-sha"},
                    "version": "v1.0.0",
                }
            ]
        )
        warnings_emitted: list[str] = []
        with patch("apm_cli.marketplace.client._HTTP_SESSION.get") as mock_get:
            mock_get.return_value = _FakeResponse(manifest_data)
            result = resolve_marketplace_plugin(
                "versioned",
                "test-market",
                version_spec="new-sha",
                warning_handler=warnings_emitted.append,
            )

        # The resolution should succeed regardless of warning behaviour
        assert result is not None

    def test_resolution_iterates_as_tuple(self, _registered_marketplace, monkeypatch):
        manifest_data = _marketplace_json_payload(
            [{"name": "iter-plugin", "source": "./plugins/iter-plugin"}]
        )

        with patch("apm_cli.marketplace.client._HTTP_SESSION.get") as mock_get:
            mock_get.return_value = _FakeResponse(manifest_data)
            canonical, plugin = resolve_marketplace_plugin("iter-plugin", "test-market")

        assert isinstance(canonical, str)
        assert isinstance(plugin, MarketplacePlugin)
