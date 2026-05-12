"""Tests for the policy checks engine (``run_policy_checks`` and individual checks)."""

from __future__ import annotations

import textwrap  # noqa: F401
from pathlib import Path

import pytest
import yaml

from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.policy.inheritance import merge_policies
from apm_cli.policy.models import CheckResult, CIAuditResult  # noqa: F401
from apm_cli.policy.policy_checks import (
    _check_compilation_strategy,
    _check_compilation_target,
    _check_dependency_allowlist,
    _check_dependency_denylist,
    _check_includes_explicit,  # noqa: F401
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

# -- Helpers --------------------------------------------------------


def _write_apm_yml(project: Path, data: dict) -> None:
    """Write apm.yml from a dict."""
    (project / "apm.yml").write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _write_lockfile(project: Path, data: dict) -> None:
    """Write apm.lock.yaml from a dict."""
    (project / "apm.lock.yaml").write_text(
        yaml.dump(data, default_flow_style=False), encoding="utf-8"
    )


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


# -- Fixtures -------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the APMPackage parse cache between tests."""
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


# -- Check 1: dependency-allowlist ----------------------------------


class TestDependencyAllowlist:
    def test_pass_when_no_allow_list(self):
        deps = _make_dep_refs(["owner/repo"])
        policy = DependencyPolicy()
        result = _check_dependency_allowlist(deps, policy)
        assert result.passed
        assert "No dependency allow list" in result.message

    def test_pass_when_in_allow_list(self):
        deps = _make_dep_refs(["owner/repo"])
        policy = DependencyPolicy(allow=["owner/*"])
        result = _check_dependency_allowlist(deps, policy)
        assert result.passed

    def test_fail_not_in_allow_list(self):
        deps = _make_dep_refs(["evil/pkg"])
        policy = DependencyPolicy(allow=["owner/*"])
        result = _check_dependency_allowlist(deps, policy)
        assert not result.passed
        assert "evil/pkg" in result.details[0]

    def test_empty_deps(self):
        policy = DependencyPolicy(allow=["owner/*"])
        result = _check_dependency_allowlist([], policy)
        assert result.passed


# -- Check 2: dependency-denylist -----------------------------------


class TestDependencyDenylist:
    def test_pass_when_no_deny_list(self):
        deps = _make_dep_refs(["owner/repo"])
        policy = DependencyPolicy()
        result = _check_dependency_denylist(deps, policy)
        assert result.passed

    def test_pass_when_not_denied(self):
        deps = _make_dep_refs(["owner/repo"])
        policy = DependencyPolicy(deny=["evil/*"])
        result = _check_dependency_denylist(deps, policy)
        assert result.passed

    def test_fail_when_denied(self):
        deps = _make_dep_refs(["evil/malware"])
        policy = DependencyPolicy(deny=["evil/*"])
        result = _check_dependency_denylist(deps, policy)
        assert not result.passed
        assert "denied by pattern" in result.details[0]


# -- Check 3: required-packages -------------------------------------


class TestRequiredPackages:
    def test_pass_no_requirements(self):
        deps = _make_dep_refs(["owner/repo"])
        policy = DependencyPolicy()
        result = _check_required_packages(deps, policy)
        assert result.passed

    def test_pass_required_present(self):
        deps = _make_dep_refs(["org/required-pkg"])
        policy = DependencyPolicy(require=["org/required-pkg"])
        result = _check_required_packages(deps, policy)
        assert result.passed

    def test_pass_required_with_version_pin(self):
        deps = _make_dep_refs(["org/required-pkg#v1.0.0"])
        policy = DependencyPolicy(require=["org/required-pkg#v2.0.0"])
        result = _check_required_packages(deps, policy)
        assert result.passed  # version checked separately in check 5

    def test_fail_required_missing(self):
        deps = _make_dep_refs(["other/pkg"])
        policy = DependencyPolicy(require=["org/required-pkg"])
        result = _check_required_packages(deps, policy)
        assert not result.passed
        assert "org/required-pkg" in result.details[0]

    def test_no_prefix_collision(self):
        """Regression: 'org/package-v2' must NOT match requirement 'org/package'."""
        deps = _make_dep_refs(["org/package-v2"])
        policy = DependencyPolicy(require=["org/package"])
        result = _check_required_packages(deps, policy)
        assert not result.passed
        assert "org/package" in result.details


# -- Check 4: required-packages-deployed ----------------------------


class TestRequiredPackagesDeployed:
    def test_pass_no_requirements(self):
        deps = _make_dep_refs(["owner/repo"])
        policy = DependencyPolicy()
        result = _check_required_packages_deployed(deps, None, policy)
        assert result.passed

    def test_pass_deployed(self):
        deps = _make_dep_refs(["org/pkg"])
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": [".github/prompts/x.md"]}])
        policy = DependencyPolicy(require=["org/pkg"])
        result = _check_required_packages_deployed(deps, lock, policy)
        assert result.passed

    def test_fail_not_deployed(self):
        deps = _make_dep_refs(["org/pkg"])
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = DependencyPolicy(require=["org/pkg"])
        result = _check_required_packages_deployed(deps, lock, policy)
        assert not result.passed
        assert "org/pkg" in result.details[0]

    def test_skip_if_not_in_manifest(self):
        """Required package not in manifest -- check 3 handles that."""
        deps = _make_dep_refs(["other/pkg"])
        lock = _make_lockfile([{"repo_url": "other/pkg", "deployed_files": ["x.md"]}])
        policy = DependencyPolicy(require=["org/missing"])
        result = _check_required_packages_deployed(deps, lock, policy)
        assert result.passed


# -- Check 5: required-package-version ------------------------------


class TestRequiredPackageVersion:
    def test_pass_no_pins(self):
        deps = _make_dep_refs(["org/pkg"])
        policy = DependencyPolicy(require=["org/pkg"])  # no #ref
        result = _check_required_package_version(deps, None, policy)
        assert result.passed

    def test_pass_version_matches(self):
        deps = _make_dep_refs(["org/pkg#v1.0.0"])
        lock = _make_lockfile([{"repo_url": "org/pkg", "resolved_ref": "v1.0.0"}])
        policy = DependencyPolicy(require=["org/pkg#v1.0.0"])
        result = _check_required_package_version(deps, lock, policy)
        assert result.passed

    def test_fail_block_mismatch(self):
        deps = _make_dep_refs(["org/pkg#v2.0.0"])
        lock = _make_lockfile([{"repo_url": "org/pkg", "resolved_ref": "v2.0.0"}])
        policy = DependencyPolicy(require=["org/pkg#v1.0.0"], require_resolution="block")
        result = _check_required_package_version(deps, lock, policy)
        assert not result.passed
        assert "expected ref 'v1.0.0'" in result.details[0]

    def test_fail_policy_wins_mismatch(self):
        deps = _make_dep_refs(["org/pkg"])
        lock = _make_lockfile([{"repo_url": "org/pkg", "resolved_ref": "v2.0.0"}])
        policy = DependencyPolicy(require=["org/pkg#v1.0.0"], require_resolution="policy-wins")
        result = _check_required_package_version(deps, lock, policy)
        assert not result.passed

    def test_pass_project_wins_mismatch(self):
        deps = _make_dep_refs(["org/pkg"])
        lock = _make_lockfile([{"repo_url": "org/pkg", "resolved_ref": "v2.0.0"}])
        policy = DependencyPolicy(require=["org/pkg#v1.0.0"], require_resolution="project-wins")
        result = _check_required_package_version(deps, lock, policy)
        assert result.passed
        # Should have warnings in details
        assert len(result.details) > 0


# -- Check 6: transitive-depth --------------------------------------


class TestTransitiveDepth:
    def test_pass_default_limit(self):
        lock = _make_lockfile([{"repo_url": "org/pkg", "depth": 3}])
        policy = DependencyPolicy()  # max_depth=50 (default)
        result = _check_transitive_depth(lock, policy)
        assert result.passed
        assert "No transitive depth limit" in result.message

    def test_pass_within_limit(self):
        lock = _make_lockfile([{"repo_url": "org/pkg", "depth": 2}])
        policy = DependencyPolicy(max_depth=3)
        result = _check_transitive_depth(lock, policy)
        assert result.passed

    def test_fail_exceeds_limit(self):
        lock = _make_lockfile([{"repo_url": "org/deep-pkg", "depth": 5}])
        policy = DependencyPolicy(max_depth=3)
        result = _check_transitive_depth(lock, policy)
        assert not result.passed
        assert "depth 5" in result.details[0]


# -- Check 7: mcp-allowlist -----------------------------------------


class TestMcpAllowlist:
    def test_pass_no_allow_list(self):
        mcp = _make_mcp_deps(["io.github.owner/server"])
        policy = McpPolicy()
        result = _check_mcp_allowlist(mcp, policy)
        assert result.passed

    def test_pass_in_allow_list(self):
        mcp = _make_mcp_deps(["io.github.owner/server"])
        policy = McpPolicy(allow=["io.github.owner/*"])
        result = _check_mcp_allowlist(mcp, policy)
        assert result.passed

    def test_fail_not_in_allow_list(self):
        mcp = _make_mcp_deps(["io.github.evil/server"])
        policy = McpPolicy(allow=["io.github.owner/*"])
        result = _check_mcp_allowlist(mcp, policy)
        assert not result.passed


# -- Check 8: mcp-denylist ------------------------------------------


class TestMcpDenylist:
    def test_pass_no_deny_list(self):
        mcp = _make_mcp_deps(["io.github.owner/server"])
        policy = McpPolicy()
        result = _check_mcp_denylist(mcp, policy)
        assert result.passed

    def test_pass_not_denied(self):
        mcp = _make_mcp_deps(["io.github.owner/server"])
        policy = McpPolicy(deny=["io.github.evil/*"])
        result = _check_mcp_denylist(mcp, policy)
        assert result.passed

    def test_fail_denied(self):
        mcp = _make_mcp_deps(["io.github.evil/server"])
        policy = McpPolicy(deny=["io.github.evil/*"])
        result = _check_mcp_denylist(mcp, policy)
        assert not result.passed
        assert "denied by pattern" in result.details[0]


# -- Check 9: mcp-transport -----------------------------------------


class TestMcpTransport:
    def test_pass_no_restrictions(self):
        mcp = _make_mcp_deps([{"name": "srv", "transport": "sse"}])
        policy = McpPolicy()
        result = _check_mcp_transport(mcp, policy)
        assert result.passed

    def test_pass_allowed_transport(self):
        mcp = _make_mcp_deps([{"name": "srv", "transport": "stdio"}])
        policy = McpPolicy(transport=McpTransportPolicy(allow=["stdio", "sse"]))
        result = _check_mcp_transport(mcp, policy)
        assert result.passed

    def test_fail_disallowed_transport(self):
        mcp = _make_mcp_deps([{"name": "srv", "transport": "http"}])
        policy = McpPolicy(transport=McpTransportPolicy(allow=["stdio"]))
        result = _check_mcp_transport(mcp, policy)
        assert not result.passed
        assert "http" in result.details[0]

    def test_skip_no_transport_set(self):
        mcp = _make_mcp_deps(["plain-server"])
        policy = McpPolicy(transport=McpTransportPolicy(allow=["stdio"]))
        result = _check_mcp_transport(mcp, policy)
        assert result.passed


# -- Check 10: mcp-self-defined -------------------------------------


class TestMcpSelfDefined:
    def test_pass_allow_policy(self):
        mcp = _make_mcp_deps(
            [{"name": "local-srv", "registry": False, "transport": "stdio", "command": "node"}]
        )
        policy = McpPolicy(self_defined="allow")
        result = _check_mcp_self_defined(mcp, policy)
        assert result.passed

    def test_pass_warn_policy(self):
        mcp = _make_mcp_deps(
            [{"name": "local-srv", "registry": False, "transport": "stdio", "command": "node"}]
        )
        policy = McpPolicy(self_defined="warn")
        result = _check_mcp_self_defined(mcp, policy)
        assert result.passed
        assert len(result.details) > 0

    def test_fail_deny_policy(self):
        mcp = _make_mcp_deps(
            [{"name": "local-srv", "registry": False, "transport": "stdio", "command": "node"}]
        )
        policy = McpPolicy(self_defined="deny")
        result = _check_mcp_self_defined(mcp, policy)
        assert not result.passed

    def test_pass_no_self_defined_servers(self):
        mcp = _make_mcp_deps(["registry-server"])
        policy = McpPolicy(self_defined="deny")
        result = _check_mcp_self_defined(mcp, policy)
        assert result.passed


# -- Check 11: compilation-target -----------------------------------


class TestCompilationTarget:
    def test_pass_no_restrictions(self):
        result = _check_compilation_target({"target": "vscode"}, CompilationPolicy())
        assert result.passed

    def test_pass_enforce_match(self):
        policy = CompilationPolicy(target=CompilationTargetPolicy(enforce="vscode"))
        result = _check_compilation_target({"target": "vscode"}, policy)
        assert result.passed

    def test_fail_enforce_mismatch(self):
        policy = CompilationPolicy(target=CompilationTargetPolicy(enforce="vscode"))
        result = _check_compilation_target({"target": "claude"}, policy)
        assert not result.passed
        assert "enforced" in result.details[0]

    def test_pass_in_allow_list(self):
        policy = CompilationPolicy(target=CompilationTargetPolicy(allow=["vscode", "claude"]))
        result = _check_compilation_target({"target": "claude"}, policy)
        assert result.passed

    def test_fail_not_in_allow_list(self):
        policy = CompilationPolicy(target=CompilationTargetPolicy(allow=["vscode"]))
        result = _check_compilation_target({"target": "claude"}, policy)
        assert not result.passed

    def test_pass_no_target_in_manifest(self):
        policy = CompilationPolicy(target=CompilationTargetPolicy(enforce="vscode"))
        result = _check_compilation_target({}, policy)
        assert result.passed

    # -- Multi-target (list) tests ----------------------------------

    def test_target_list_enforce_present(self):
        """List target containing the enforced value passes."""
        policy = CompilationPolicy(target=CompilationTargetPolicy(enforce="claude"))
        result = _check_compilation_target({"target": ["claude", "copilot"]}, policy)
        assert result.passed

    def test_target_list_enforce_missing(self):
        """List target missing the enforced value fails."""
        policy = CompilationPolicy(target=CompilationTargetPolicy(enforce="claude"))
        result = _check_compilation_target({"target": ["cursor", "copilot"]}, policy)
        assert not result.passed
        assert "enforced" in result.details[0]

    def test_target_list_allow_all_in(self):
        """All items in list target within allow set passes."""
        policy = CompilationPolicy(
            target=CompilationTargetPolicy(allow=["claude", "copilot", "cursor"])
        )
        result = _check_compilation_target({"target": ["claude", "copilot"]}, policy)
        assert result.passed

    def test_target_list_allow_some_disallowed(self):
        """List target with items outside allow set fails."""
        policy = CompilationPolicy(target=CompilationTargetPolicy(allow=["claude"]))
        result = _check_compilation_target({"target": ["claude", "copilot"]}, policy)
        assert not result.passed
        assert "copilot" in result.message

    def test_target_string_still_works(self):
        """Backward compat: single string target with enforce."""
        policy = CompilationPolicy(target=CompilationTargetPolicy(enforce="copilot"))
        result = _check_compilation_target({"target": "copilot"}, policy)
        assert result.passed

    def test_target_list_single_item(self):
        """Single-element list target with matching enforce passes."""
        policy = CompilationPolicy(target=CompilationTargetPolicy(enforce="copilot"))
        result = _check_compilation_target({"target": ["copilot"]}, policy)
        assert result.passed


# -- Check 12: compilation-strategy ---------------------------------


class TestCompilationStrategy:
    def test_pass_no_enforce(self):
        result = _check_compilation_strategy(
            {"compilation": {"strategy": "distributed"}}, CompilationPolicy()
        )
        assert result.passed

    def test_pass_enforce_match(self):
        policy = CompilationPolicy(strategy=CompilationStrategyPolicy(enforce="distributed"))
        result = _check_compilation_strategy({"compilation": {"strategy": "distributed"}}, policy)
        assert result.passed

    def test_fail_enforce_mismatch(self):
        policy = CompilationPolicy(strategy=CompilationStrategyPolicy(enforce="single-file"))
        result = _check_compilation_strategy({"compilation": {"strategy": "distributed"}}, policy)
        assert not result.passed

    def test_pass_no_strategy_in_manifest(self):
        policy = CompilationPolicy(strategy=CompilationStrategyPolicy(enforce="distributed"))
        result = _check_compilation_strategy({}, policy)
        assert result.passed


# -- Check 13: source-attribution -----------------------------------


class TestSourceAttribution:
    def test_pass_not_required(self):
        result = _check_source_attribution({}, CompilationPolicy())
        assert result.passed

    def test_pass_enabled(self):
        policy = CompilationPolicy(source_attribution=True)
        result = _check_source_attribution({"compilation": {"source_attribution": True}}, policy)
        assert result.passed

    def test_fail_not_enabled(self):
        policy = CompilationPolicy(source_attribution=True)
        result = _check_source_attribution({}, policy)
        assert not result.passed


# -- Check 14: required-manifest-fields -----------------------------


class TestRequiredManifestFields:
    def test_pass_no_requirements(self):
        result = _check_required_manifest_fields({}, ManifestPolicy())
        assert result.passed

    def test_pass_fields_present(self):
        policy = ManifestPolicy(required_fields=["description", "author"])
        result = _check_required_manifest_fields({"description": "A pkg", "author": "Me"}, policy)
        assert result.passed

    def test_fail_field_missing(self):
        policy = ManifestPolicy(required_fields=["description", "license"])
        result = _check_required_manifest_fields({"description": "A pkg"}, policy)
        assert not result.passed
        assert "license" in result.details

    def test_fail_field_empty(self):
        policy = ManifestPolicy(required_fields=["description"])
        result = _check_required_manifest_fields({"description": ""}, policy)
        assert not result.passed


# -- Check 15: scripts-policy ---------------------------------------


class TestScriptsPolicy:
    def test_pass_scripts_allowed(self):
        result = _check_scripts_policy({"scripts": {"build": "echo hi"}}, ManifestPolicy())
        assert result.passed

    def test_pass_deny_no_scripts(self):
        policy = ManifestPolicy(scripts="deny")
        result = _check_scripts_policy({}, policy)
        assert result.passed

    def test_fail_deny_with_scripts(self):
        policy = ManifestPolicy(scripts="deny")
        result = _check_scripts_policy({"scripts": {"build": "echo hi"}}, policy)
        assert not result.passed
        assert "build" in result.details


# -- Check 16: unmanaged-files --------------------------------------


class TestUnmanagedFiles:
    def test_pass_ignore_action(self, tmp_path):
        policy = UnmanagedFilesPolicy(action="ignore")
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert result.passed

    def test_pass_no_unmanaged(self, tmp_path):
        # Create a governance file that IS in the lockfile
        (tmp_path / ".github" / "prompts").mkdir(parents=True)
        (tmp_path / ".github" / "prompts" / "x.md").write_text("hi", encoding="utf-8")
        lock = _make_lockfile(
            [
                {
                    "repo_url": "org/pkg",
                    "deployed_files": [".github/prompts/x.md"],
                }
            ]
        )
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/prompts"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert result.passed

    def test_fail_deny_unmanaged(self, tmp_path):
        # Create a governance file NOT in lockfile
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "rogue.md").write_text("rogue", encoding="utf-8")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert not result.passed
        assert ".github/agents/rogue.md" in result.details

    def test_warn_unmanaged(self, tmp_path):
        (tmp_path / ".cursor" / "rules").mkdir(parents=True)
        (tmp_path / ".cursor" / "rules" / "extra.md").write_text("extra", encoding="utf-8")
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(action="warn", directories=[".cursor/rules"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert result.passed
        assert len(result.details) > 0

    def test_pass_empty_directory(self, tmp_path):
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert result.passed

    def test_pass_files_under_deployed_directory(self, tmp_path):
        """Files under a deployed directory (trailing /) are treated as managed."""
        skill_dir = tmp_path / ".github" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("skill", encoding="utf-8")
        (skill_dir / "prompt.md").write_text("prompt", encoding="utf-8")
        lock = _make_lockfile(
            [
                {
                    "repo_url": "org/pkg",
                    "deployed_files": [".github/skills/my-skill/"],
                }
            ]
        )
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/skills"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert result.passed

    def test_rglob_cap_skips_check(self, tmp_path, monkeypatch):
        """When file count exceeds the safety cap, check passes with a warning."""
        from apm_cli.policy import policy_checks

        monkeypatch.setattr(policy_checks, "_MAX_UNMANAGED_SCAN_FILES", 3)
        gov = tmp_path / ".github" / "agents"
        gov.mkdir(parents=True)
        for i in range(5):
            (gov / f"file{i}.md").write_text("x", encoding="utf-8")
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert result.passed
        assert "capped" in result.message.lower()

    # -- Regression tests for #1199 ------------------------------------

    def test_handrolled_instruction_flagged_as_unmanaged(self, tmp_path: Path) -> None:
        """Hand-rolled files in .github/instructions/ are flagged as unmanaged.

        Regression guard for #1199: a file that exists in the governance
        directory but is absent from every dependency's deployed_files must
        be reported as unmanaged when action='deny'.
        """
        instructions_dir = tmp_path / ".github" / "instructions"
        instructions_dir.mkdir(parents=True)
        # A file managed by a dependency.
        (instructions_dir / "managed.instructions.md").write_text("managed", encoding="utf-8")
        # A hand-rolled file not tracked by any dependency.
        (instructions_dir / "handrolled.instructions.md").write_text(
            "hand-rolled", encoding="utf-8"
        )
        lock = _make_lockfile(
            [
                {
                    "repo_url": "org/pkg",
                    "deployed_files": [".github/instructions/managed.instructions.md"],
                }
            ]
        )
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/instructions"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert not result.passed
        assert ".github/instructions/handrolled.instructions.md" in result.details

    def test_handrolled_file_not_masked_by_deployed_deps(self, tmp_path: Path) -> None:
        """A hand-rolled file is flagged even when sibling files are managed.

        Regression guard for #1199: the presence of dependency-deployed files
        in the same directory must not suppress the report for adjacent
        hand-rolled files.
        """
        instructions_dir = tmp_path / ".github" / "instructions"
        instructions_dir.mkdir(parents=True)
        # Two managed files deployed by the dependency.
        (instructions_dir / "a.instructions.md").write_text("a", encoding="utf-8")
        (instructions_dir / "b.instructions.md").write_text("b", encoding="utf-8")
        # One hand-rolled file alongside the managed ones.
        (instructions_dir / "rogue.instructions.md").write_text("rogue", encoding="utf-8")
        lock = _make_lockfile(
            [
                {
                    "repo_url": "org/team-pkg",
                    "deployed_files": [
                        ".github/instructions/a.instructions.md",
                        ".github/instructions/b.instructions.md",
                    ],
                }
            ]
        )
        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/instructions"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert not result.passed
        assert ".github/instructions/rogue.instructions.md" in result.details
        # Managed files must NOT appear in the unmanaged details list.
        assert ".github/instructions/a.instructions.md" not in result.details
        assert ".github/instructions/b.instructions.md" not in result.details

    def test_handrolled_file_across_governance_dirs(self, tmp_path: Path) -> None:
        """Hand-rolled files in instructions/, hooks/, and agents/ are all flagged.

        Regression guard for #1199: the audit must behave consistently across
        every governance directory, not just .github/agents/.
        """
        for subdir in ("instructions", "hooks", "agents"):
            target = tmp_path / ".github" / subdir
            target.mkdir(parents=True)
            (target / f"handrolled.{subdir}.md").write_text("hand-rolled", encoding="utf-8")
        # No dependency deploys any of these files.
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = UnmanagedFilesPolicy(
            action="deny",
            directories=[
                ".github/instructions",
                ".github/hooks",
                ".github/agents",
            ],
        )
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert not result.passed
        assert ".github/instructions/handrolled.instructions.md" in result.details
        assert ".github/hooks/handrolled.hooks.md" in result.details
        assert ".github/agents/handrolled.agents.md" in result.details

    def test_local_deployed_files_not_flagged(self, tmp_path: Path) -> None:
        """Files in local_deployed_files are treated as managed and not flagged.

        Regression guard for #1199: the virtual _SELF_KEY dependency that APM
        synthesises from local_deployed_files must exempt those paths from the
        unmanaged-files check.
        """
        from apm_cli.deps.lockfile import LockFile

        instructions_dir = tmp_path / ".github" / "instructions"
        instructions_dir.mkdir(parents=True)
        managed_path = ".github/instructions/managed.instructions.md"
        (tmp_path / managed_path).write_text("managed locally", encoding="utf-8")

        # Build a LockFile with local_deployed_files then round-trip through
        # YAML so the _SELF_KEY virtual entry is synthesised.
        lock = LockFile()
        lock.local_deployed_files = [managed_path]
        lock_file = tmp_path / "apm.lock.yaml"
        lock.write(lock_file)
        lock = LockFile.read(lock_file)

        policy = UnmanagedFilesPolicy(action="deny", directories=[".github/instructions"])
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert result.passed
        assert managed_path not in result.details

    def test_dir_prefix_does_not_mask_instructions(self, tmp_path: Path) -> None:
        """A trailing-slash directory entry only covers its own subtree.

        Regression guard for #1199: a deployed directory prefix such as
        '.github/skills/my-skill/' must not suppress unmanaged-file detection
        in a different governance directory (.github/instructions/).
        """
        skills_dir = tmp_path / ".github" / "skills" / "my-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("skill content", encoding="utf-8")

        instructions_dir = tmp_path / ".github" / "instructions"
        instructions_dir.mkdir(parents=True)
        (instructions_dir / "handrolled.instructions.md").write_text(
            "hand-rolled", encoding="utf-8"
        )

        lock = _make_lockfile(
            [
                {
                    "repo_url": "org/skills-pkg",
                    # Trailing slash marks an entire directory as managed.
                    "deployed_files": [".github/skills/my-skill/"],
                }
            ]
        )
        policy = UnmanagedFilesPolicy(
            action="deny",
            directories=[".github/skills", ".github/instructions"],
        )
        result = _check_unmanaged_files(tmp_path, lock, policy)
        assert not result.passed
        # The hand-rolled instruction file must be flagged...
        assert ".github/instructions/handrolled.instructions.md" in result.details
        # ...while files under the managed skill directory must NOT be flagged.
        assert ".github/skills/my-skill/SKILL.md" not in result.details

    def test_action_none_resolves_to_ignore(self, tmp_path):
        """action=None standalone: effective_action resolves to 'ignore', check passes."""
        policy = UnmanagedFilesPolicy(action=None)
        result = _check_unmanaged_files(tmp_path, None, policy)
        assert result.passed

    def test_action_none_inherits_parent_deny(self, tmp_path):
        """action=None + parent deny: merge propagates deny to child."""
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "rogue.md").write_text("rogue", encoding="utf-8")
        parent = ApmPolicy(
            unmanaged_files=UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        )
        child = ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action=None))
        merged = merge_policies(parent, child)
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        result = _check_unmanaged_files(tmp_path, lock, merged.unmanaged_files)
        assert not result.passed

    def test_rogue_file_caught_parent_deny_child_omits_block(self, tmp_path):
        """Rogue file is caught when parent says deny but child omits unmanaged_files block."""
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "rogue.md").write_text("rogue", encoding="utf-8")
        parent = ApmPolicy(
            unmanaged_files=UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        )
        child = ApmPolicy()  # child omits unmanaged_files entirely (defaults to action=None)
        merged = merge_policies(parent, child)
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        result = _check_unmanaged_files(tmp_path, lock, merged.unmanaged_files)
        assert not result.passed
        assert ".github/agents/rogue.md" in result.details

    def test_integration_chain_deny_propagates(self, tmp_path):
        """Integration chain: parent deny + child omits block -> merge -> check catches rogue file."""
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "rogue.md").write_text("rogue", encoding="utf-8")
        parent_policy = ApmPolicy(
            unmanaged_files=UnmanagedFilesPolicy(action="deny", directories=[".github/agents"])
        )
        child_policy = ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action=None))
        merged = merge_policies(parent_policy, child_policy)
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        result = _check_unmanaged_files(tmp_path, lock, merged.unmanaged_files)
        assert not result.passed
        assert ".github/agents/rogue.md" in result.details


# -- Integration: run_policy_checks ---------------------------------


class TestRunPolicyChecks:
    def test_returns_all_17_checks(self, tmp_path):
        """Full run should produce exactly 17 checks."""
        _write_apm_yml(
            tmp_path,
            {
                "name": "test",
                "version": "1.0.0",
                "dependencies": {"apm": ["owner/repo"]},
            },
        )
        _write_lockfile(
            tmp_path,
            {
                "lockfile_version": "1",
                "generated_at": "2025-01-01T00:00:00Z",
                "dependencies": [
                    {"repo_url": "owner/repo", "deployed_files": [".github/prompts/x.md"]}
                ],
            },
        )
        (tmp_path / ".github" / "prompts").mkdir(parents=True)
        (tmp_path / ".github" / "prompts" / "x.md").write_text("ok", encoding="utf-8")

        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert len(result.checks) == 17
        # Default policy = all checks pass
        assert result.passed

    def test_mixed_pass_fail(self, tmp_path):
        """Denied dep should fail while other checks pass."""
        _write_apm_yml(
            tmp_path,
            {
                "name": "test",
                "version": "1.0.0",
                "dependencies": {"apm": ["evil/malware"]},
            },
        )
        _write_lockfile(
            tmp_path,
            {
                "lockfile_version": "1",
                "generated_at": "2025-01-01T00:00:00Z",
                "dependencies": [{"repo_url": "evil/malware", "deployed_files": ["x.md"]}],
            },
        )

        policy = ApmPolicy(dependencies=DependencyPolicy(deny=["evil/*"]))
        result = run_policy_checks(tmp_path, policy)
        assert not result.passed
        failed_names = {c.name for c in result.checks if not c.passed}
        assert "dependency-denylist" in failed_names

    def test_no_apm_yml_returns_empty(self, tmp_path):
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert len(result.checks) == 0
        assert result.passed

    def test_multiple_failures(self, tmp_path):
        """Multiple policy violations accumulate."""
        _write_apm_yml(
            tmp_path,
            {
                "name": "test",
                "version": "1.0.0",
                "dependencies": {"apm": ["evil/pkg"]},
                "scripts": {"build": "make"},
            },
        )
        _write_lockfile(
            tmp_path,
            {
                "lockfile_version": "1",
                "generated_at": "2025-01-01T00:00:00Z",
                "dependencies": [{"repo_url": "evil/pkg", "deployed_files": ["x.md"]}],
            },
        )

        policy = ApmPolicy(
            dependencies=DependencyPolicy(deny=["evil/*"]),
            manifest=ManifestPolicy(scripts="deny", required_fields=["license"]),
        )
        result = run_policy_checks(tmp_path, policy, fail_fast=False)
        failed_names = {c.name for c in result.checks if not c.passed}
        assert "dependency-denylist" in failed_names
        assert "scripts-policy" in failed_names
        assert "required-manifest-fields" in failed_names


# -- Group 1: _load_raw_apm_yml malformed-manifest tests -----------


class TestLoadRawApmYml:
    """Tests for _load_raw_apm_yml with malformed content (fix #936)."""

    def test_malformed_yaml_returns_none_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Malformed YAML returns None and logs a WARNING."""
        (tmp_path / "apm.yml").write_text(": :\n  bad: [yaml\n", encoding="utf-8")
        import logging

        with caplog.at_level(logging.WARNING, logger="apm_cli.policy.policy_checks"):
            result = _load_raw_apm_yml(tmp_path)
        assert result is None
        assert "Malformed YAML" in caplog.text

    def test_non_dict_yaml_returns_none_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-mapping YAML (bare list) returns None and logs a WARNING."""
        (tmp_path / "apm.yml").write_text("- item1\n- item2\n", encoding="utf-8")
        import logging

        with caplog.at_level(logging.WARNING, logger="apm_cli.policy.policy_checks"):
            result = _load_raw_apm_yml(tmp_path)
        assert result is None
        assert "not a YAML mapping" in caplog.text

    def test_scalar_yaml_returns_none_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Plain scalar YAML returns None and logs a WARNING."""
        (tmp_path / "apm.yml").write_text("just a string\n", encoding="utf-8")
        import logging

        with caplog.at_level(logging.WARNING, logger="apm_cli.policy.policy_checks"):
            result = _load_raw_apm_yml(tmp_path)
        assert result is None
        assert "not a YAML mapping" in caplog.text

    def test_empty_file_returns_none_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty apm.yml (yaml.safe_load returns None) is not a dict."""
        (tmp_path / "apm.yml").write_text("", encoding="utf-8")
        import logging

        with caplog.at_level(logging.WARNING, logger="apm_cli.policy.policy_checks"):
            result = _load_raw_apm_yml(tmp_path)
        assert result is None
        assert "not a YAML mapping" in caplog.text

    def test_missing_file_returns_none_silently(self, tmp_path: Path) -> None:
        """Missing apm.yml returns None without raising or logging."""
        result = _load_raw_apm_yml(tmp_path)
        assert result is None

    def test_valid_yaml_returns_dict(self, tmp_path: Path) -> None:
        """Valid YAML mapping is returned as a dict unchanged."""
        (tmp_path / "apm.yml").write_text("name: test\nversion: '1.0'\n", encoding="utf-8")
        result = _load_raw_apm_yml(tmp_path)
        assert result == {"name": "test", "version": "1.0"}


# -- Group 2: run_policy_checks malformed-manifest tests -----------


class TestRunPolicyChecksMalformedManifest:
    """run_policy_checks must fail-closed on malformed apm.yml (fix #936)."""

    def test_malformed_yaml_produces_failing_check(self, tmp_path: Path) -> None:
        """Malformed YAML appends manifest-parse check with passed=False."""
        (tmp_path / "apm.yml").write_text(": :\n  bad: [yaml\n", encoding="utf-8")
        clear_apm_yml_cache()
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert not result.passed
        parse_checks = [c for c in result.checks if c.name == "manifest-parse"]
        assert len(parse_checks) == 1
        assert not parse_checks[0].passed
        assert "Cannot parse apm.yml" in parse_checks[0].message

    def test_non_dict_yaml_produces_failing_check(self, tmp_path: Path) -> None:
        """Non-dict YAML (bare list) triggers a manifest-parse failing check."""
        (tmp_path / "apm.yml").write_text("- item1\n- item2\n", encoding="utf-8")
        clear_apm_yml_cache()
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert not result.passed
        parse_checks = [c for c in result.checks if c.name == "manifest-parse"]
        assert len(parse_checks) == 1
        assert not parse_checks[0].passed

    def test_empty_file_produces_failing_check(self, tmp_path: Path) -> None:
        """Empty apm.yml triggers ValueError from from_apm_yml."""
        (tmp_path / "apm.yml").write_text("", encoding="utf-8")
        clear_apm_yml_cache()
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert not result.passed
        parse_checks = [c for c in result.checks if c.name == "manifest-parse"]
        assert len(parse_checks) == 1
        assert not parse_checks[0].passed

    def test_missing_apm_yml_returns_empty_passing_result(self, tmp_path: Path) -> None:
        """No apm.yml means nothing to check -- result is empty and passes."""
        clear_apm_yml_cache()
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert result.passed
        assert len(result.checks) == 0

    # -- Group 5 regression guard --

    def test_malformed_yaml_does_not_silently_pass(self, tmp_path: Path) -> None:
        """Regression: malformed YAML must NOT produce an empty passing result.

        Before fix #936, malformed apm.yml returned CIAuditResult() with
        no checks, which trivially passed (all([]) is True).
        """
        (tmp_path / "apm.yml").write_text("{{invalid yaml}}\n", encoding="utf-8")
        clear_apm_yml_cache()
        policy = ApmPolicy()
        result = run_policy_checks(tmp_path, policy)
        assert not result.passed, "Malformed apm.yml must not silently pass policy checks"
