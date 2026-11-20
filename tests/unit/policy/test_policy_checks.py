"""Tests for the policy checks engine (``run_policy_checks`` and individual checks)."""

from __future__ import annotations

import textwrap  # noqa: F401

import pytest

from apm_cli.models.apm_package import clear_apm_yml_cache
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
    _check_required_executable_untrusted,
    _check_required_manifest_fields,
    _check_required_package_version,
    _check_required_packages,
    _check_required_packages_deployed,
    _check_scripts_policy,
    _check_source_attribution,
    _check_transitive_depth,
)
from apm_cli.policy.schema import (
    CompilationPolicy,
    CompilationStrategyPolicy,
    CompilationTargetPolicy,
    DependencyPolicy,
    ExecutablesPolicy,
    ManifestPolicy,
    McpPolicy,
    McpTransportPolicy,
)

pytestmark = pytest.mark.unit

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

    def test_pass_present_but_parked(self):
        """Gap B (#1873): a package present in the lockfile with NO deployed
        files (executables gated pending approval) is PRESENT and must pass --
        the old deployed_files test mis-fired on this healthy parked state."""
        deps = _make_dep_refs(["org/pkg"])
        lock = _make_lockfile([{"repo_url": "org/pkg", "deployed_files": []}])
        policy = DependencyPolicy(require=["org/pkg"])
        result = _check_required_packages_deployed(deps, lock, policy)
        assert result.passed

    def test_fail_absent_from_lockfile(self):
        """A required package in the manifest but absent from the lockfile is a
        genuine presence failure and includes the --no-policy recovery hint."""
        deps = _make_dep_refs(["org/pkg"])
        lock = _make_lockfile([{"repo_url": "other/pkg", "deployed_files": ["x.md"]}])
        policy = DependencyPolicy(require=["org/pkg"])
        result = _check_required_packages_deployed(deps, lock, policy)
        assert not result.passed
        assert "org/pkg" in result.details[0]
        assert "--no-policy" in result.message

    def test_fail_if_not_in_manifest(self):
        """An entry naming no declared package is not skipped (#3993)."""
        deps = _make_dep_refs(["other/pkg"])
        lock = _make_lockfile([{"repo_url": "other/pkg", "deployed_files": ["x.md"]}])
        policy = DependencyPolicy(require=["org/missing"])
        result = _check_required_packages_deployed(deps, lock, policy)
        assert not result.passed
        assert result.details == ["org/missing"]


# -- Check 4b: required-executable-untrusted ------------------------


class TestRequiredExecutableUntrusted:
    def test_pass_no_required_executables(self):
        deps = _make_dep_refs(["org/pkg"])
        lock = _make_lockfile([{"repo_url": "org/pkg"}])
        result = _check_required_executable_untrusted(deps, lock, ExecutablesPolicy())
        assert result.passed

    def test_pass_required_executable_deployed(self):
        deps = _make_dep_refs(["org/pkg"])
        lock = _make_lockfile([{"repo_url": "org/pkg", "exec_status": "deployed"}])
        result = _check_required_executable_untrusted(
            deps, lock, ExecutablesPolicy(require=("org/pkg",))
        )
        assert result.passed

    def test_fail_required_executable_parked(self):
        deps = _make_dep_refs(["org/pkg"])
        lock = _make_lockfile([{"repo_url": "org/pkg", "exec_status": "gated_pending_approval"}])
        result = _check_required_executable_untrusted(
            deps, lock, ExecutablesPolicy(require=("org/pkg",))
        )
        assert not result.passed
        assert "org/pkg" in result.details
        assert "apm approve" in result.message

    def test_fail_required_executable_denied(self):
        deps = _make_dep_refs(["org/pkg"])
        lock = _make_lockfile([{"repo_url": "org/pkg", "exec_status": "denied"}])
        result = _check_required_executable_untrusted(
            deps, lock, ExecutablesPolicy(require=("org/pkg",))
        )
        assert not result.passed

    def test_skip_if_not_in_manifest(self):
        deps = _make_dep_refs(["other/pkg"])
        lock = _make_lockfile([{"repo_url": "other/pkg", "exec_status": "deployed"}])
        result = _check_required_executable_untrusted(
            deps, lock, ExecutablesPolicy(require=("org/missing",))
        )
        assert result.passed

    def test_require_only_message_omits_recommended_noop(self):
        # S2 (#1873): a require-only package (not in recommend) must NOT be
        # told to run `apm approve --recommended` -- that is a no-op when the
        # recommend set is empty. Lead with the per-package remedy instead.
        deps = _make_dep_refs(["org/pkg"])
        lock = _make_lockfile([{"repo_url": "org/pkg", "exec_status": "gated_pending_approval"}])
        result = _check_required_executable_untrusted(
            deps, lock, ExecutablesPolicy(require=("org/pkg",))
        )
        assert not result.passed
        assert "apm approve <package>" in result.message
        assert "--recommended" not in result.message

    def test_require_and_recommend_message_offers_bulk(self):
        # When the untrusted required package IS in the recommend set, the
        # bulk one-liner is genuinely actionable, so surface it.
        deps = _make_dep_refs(["org/pkg"])
        lock = _make_lockfile([{"repo_url": "org/pkg", "exec_status": "gated_pending_approval"}])
        result = _check_required_executable_untrusted(
            deps,
            lock,
            ExecutablesPolicy(require=("org/pkg",), recommend=("org/pkg",)),
        )
        assert not result.passed
        assert "--recommended" in result.message


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
    """Check 9 judges canonical effective transports supplied by the owner."""

    def test_pass_no_restrictions(self):
        result = _check_mcp_transport({"srv": frozenset({"sse"})}, McpPolicy())
        assert result.passed

    def test_pass_allowed_transport(self):
        policy = McpPolicy(transport=McpTransportPolicy(allow=("stdio", "sse")))
        result = _check_mcp_transport({"srv": frozenset({"stdio"})}, policy)
        assert result.passed

    def test_fail_disallowed_transport(self):
        policy = McpPolicy(transport=McpTransportPolicy(allow=("stdio",)))
        result = _check_mcp_transport({"srv": frozenset({"streamable-http"})}, policy)
        assert not result.passed
        assert result.details == ["srv: transport 'streamable-http' not in allowed [stdio]"]

    def test_package_and_remote_both_checked(self):
        policy = McpPolicy(transport=McpTransportPolicy(allow=("stdio", "streamable-http")))
        result = _check_mcp_transport({"srv": frozenset({"stdio", "sse"})}, policy)
        assert not result.passed
        assert result.details == ["srv: transport 'sse' not in allowed [stdio, streamable-http]"]

    def test_unresolved_transport_fails_with_remedy(self):
        from apm_cli.mcp_transport import UnresolvedTransport

        policy = McpPolicy(transport=McpTransportPolicy(allow=("stdio",)))
        result = _check_mcp_transport(
            {"srv": UnresolvedTransport("MCP registry https://r.example is unreachable")},
            policy,
        )
        assert not result.passed
        detail = result.details[0]
        assert detail.startswith("srv: effective transport unknown (MCP registry")
        assert "unreachable" in detail
        assert "make the MCP registry reachable, then retry" in detail
        assert "A declared 'transport:' cannot stand in" in detail


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


def _decision_targets(value):
    """Project a target selection the way every caller of the check must."""
    from apm_cli.core.target_detection import EffectiveTargetDecision

    return EffectiveTargetDecision(value, "apm.yml").canonical_targets


def _target_policy(*, allow=None, enforce=None) -> CompilationPolicy:
    return CompilationPolicy(target=CompilationTargetPolicy(allow=allow, enforce=enforce))


class TestCompilationTarget:
    def test_pass_no_restrictions(self):
        result = _check_compilation_target(_decision_targets("vscode"), CompilationPolicy())
        assert result.passed

    def test_pass_enforce_match(self):
        policy = _target_policy(enforce="vscode")
        result = _check_compilation_target(_decision_targets("vscode"), policy)
        assert result.passed

    def test_fail_enforce_mismatch(self):
        policy = _target_policy(enforce="vscode")
        result = _check_compilation_target(_decision_targets("claude"), policy)
        assert not result.passed
        assert "enforced" in result.details[0]

    def test_pass_in_allow_list(self):
        policy = _target_policy(allow=["vscode", "claude"])
        result = _check_compilation_target(_decision_targets("claude"), policy)
        assert result.passed

    def test_fail_not_in_allow_list(self):
        policy = _target_policy(allow=["vscode"])
        result = _check_compilation_target(_decision_targets("claude"), policy)
        assert not result.passed

    def test_pass_no_target_in_manifest(self):
        policy = _target_policy(enforce="vscode")
        result = _check_compilation_target(None, policy)
        assert result.passed

    # -- Multi-target (list) tests ----------------------------------

    def test_target_list_enforce_present(self):
        """List target containing the enforced value passes."""
        policy = _target_policy(enforce="claude")
        result = _check_compilation_target(_decision_targets(["claude", "copilot"]), policy)
        assert result.passed

    def test_target_list_enforce_missing(self):
        """List target missing the enforced value fails."""
        policy = _target_policy(enforce="claude")
        result = _check_compilation_target(_decision_targets(["cursor", "copilot"]), policy)
        assert not result.passed
        assert "enforced" in result.details[0]

    def test_target_list_allow_all_in(self):
        """All items in list target within allow set passes."""
        policy = _target_policy(allow=["claude", "copilot", "cursor"])
        result = _check_compilation_target(_decision_targets(["claude", "copilot"]), policy)
        assert result.passed

    def test_target_list_allow_some_disallowed(self):
        """List target with items outside allow set fails."""
        policy = _target_policy(allow=["claude"])
        result = _check_compilation_target(_decision_targets(["claude", "copilot"]), policy)
        assert not result.passed
        assert "copilot" in result.message

    def test_target_list_single_item(self):
        """Single-element list target with matching enforce passes."""
        policy = _target_policy(enforce="copilot")
        result = _check_compilation_target(_decision_targets(["copilot"]), policy)
        assert result.passed

    # -- Canonical comparison (#3732) -------------------------------

    @pytest.mark.parametrize("spelling", ["vscode", "agents", "copilot", ["vscode"]])
    def test_alias_matches_canonical_allow(self, spelling):
        """Aliases compare as the profile they deploy, on the manifest side."""
        policy = _target_policy(allow=["claude", "copilot"])
        assert _check_compilation_target(_decision_targets(spelling), policy).passed

    def test_policy_alias_matches_canonical_target(self):
        """Aliases also compare as profiles on the policy side."""
        policy = _target_policy(allow=["vscode"], enforce=None)
        assert _check_compilation_target(_decision_targets("copilot"), policy).passed
        policy = _target_policy(enforce="vscode")
        assert _check_compilation_target(_decision_targets(["claude", "copilot"]), policy).passed

    def test_mcp_only_target_judged_as_its_profile(self):
        """intellij configures Copilot, so policy judges it as copilot."""
        policy = _target_policy(allow=["copilot"])
        assert _check_compilation_target(_decision_targets("intellij"), policy).passed

    def test_all_in_allow_is_unrestricted(self):
        """``allow: [all]`` permits every target."""
        policy = _target_policy(allow=["all"])
        assert _check_compilation_target(_decision_targets(["claude", "cursor"]), policy).passed

    def test_singular_all_judges_the_install_expansion(self):
        """``target: all`` deploys everywhere, so a narrow allow list fails."""
        policy = _target_policy(allow=["claude", "copilot"])
        result = _check_compilation_target(_decision_targets("all"), policy)
        assert not result.passed
        assert "'all'" not in result.message
        assert "cursor" in result.message

    def test_enforce_all_requires_the_full_expansion(self):
        policy = _target_policy(enforce="all")
        assert _check_compilation_target(_decision_targets("all"), policy).passed
        assert not _check_compilation_target(_decision_targets("claude"), policy).passed

    def test_violation_names_the_selection_source(self):
        policy = _target_policy(allow=["claude"])
        result = _check_compilation_target(
            _decision_targets(["claude", "cursor"]), policy, source="--target flag"
        )
        assert "--target flag" in result.details[0]


# -- Check 12: compilation-strategy ---------------------------------


class TestCompilationStrategy:
    @pytest.mark.parametrize(
        ("manifest", "enforce", "passed"),
        [
            ({"compilation": {"strategy": "distributed"}}, None, True),
            ({"compilation": {"strategy": "distributed"}}, "distributed", True),
            ({"compilation": {"strategy": "distributed"}}, "single-file", False),
            ({}, "distributed", True),
        ],
        ids=["pass-no-enforce", "pass-enforce-match", "fail-enforce-mismatch", "pass-no-strategy"],
    )
    def test_strategy_enforcement(self, manifest, enforce, passed):
        policy = (
            CompilationPolicy()
            if enforce is None
            else CompilationPolicy(strategy=CompilationStrategyPolicy(enforce=enforce))
        )
        assert _check_compilation_strategy(manifest, policy).passed is passed


# -- Check 13: source-attribution -----------------------------------


class TestSourceAttribution:
    @pytest.mark.parametrize(
        ("manifest", "required", "passed"),
        [
            ({}, False, True),
            ({"compilation": {"source_attribution": True}}, True, True),
            ({}, True, False),
        ],
        ids=["pass-not-required", "pass-enabled", "fail-not-enabled"],
    )
    def test_source_attribution_requirement(self, manifest, required, passed):
        policy = CompilationPolicy(source_attribution=True) if required else CompilationPolicy()
        assert _check_source_attribution(manifest, policy).passed is passed


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


# -- Check 16b: enriched unmanaged-files (#1775) --------------------


class TestUnmanagedClassification:
    """Lazy primitive-type classification of already-flagged files."""

    def test_classify_skill_dir(self):
        from apm_cli.policy.policy_checks import _classify_primitive_type

        assert _classify_primitive_type(".claude/skills/my-skill/helper.md") == "skill"

    def test_classify_instruction_file(self):
        from apm_cli.policy.policy_checks import _classify_primitive_type

        assert _classify_primitive_type(".github/instructions/x.instructions.md") == "instruction"

    def test_classify_agent_dir(self):
        from apm_cli.policy.policy_checks import _classify_primitive_type

        assert _classify_primitive_type(".github/agents/reviewer.agent.md") == "agent"

    def test_classify_mcp_config(self):
        from apm_cli.policy.policy_checks import _classify_primitive_type

        assert _classify_primitive_type(".vscode/mcp.json") == "mcp"

    def test_classify_agent_in_mcp_named_dir(self):
        # A path segment literally named "mcp" must NOT override an explicit
        # .agent.md filename convention.
        from apm_cli.policy.policy_checks import _classify_primitive_type

        assert _classify_primitive_type(".github/agents/mcp/rogue.agent.md") == "agent"

    def test_classify_dot_mcp_root_config(self):
        from apm_cli.policy.policy_checks import _classify_primitive_type

        assert _classify_primitive_type(".mcp/servers.json") == "mcp"

    def test_classify_plain_mcp_segment_not_mcp(self):
        # A non-config file under a dir merely named "mcp" is not an MCP config.
        from apm_cli.policy.policy_checks import _classify_primitive_type

        assert _classify_primitive_type(".github/mcp/notes.txt") is None

    def test_classify_unknown_returns_none(self):
        from apm_cli.policy.policy_checks import _classify_primitive_type

        assert _classify_primitive_type(".github/random/notes.txt") is None


# -- Group 1: _load_raw_apm_yml malformed-manifest tests -----------


# -- Group 2: run_policy_checks malformed-manifest tests -----------
