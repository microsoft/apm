"""Tests for policy inheritance chain resolution and merge logic."""

from __future__ import annotations

import dataclasses
import unittest
from typing import ClassVar

from apm_cli.policy.inheritance import (
    MAX_CHAIN_DEPTH,
    PolicyInheritanceError,
    detect_cycle,
    merge_policies,
    resolve_policy_chain,
    validate_chain_depth,
)
from apm_cli.policy.schema import (
    ApmPolicy,
    AuditPolicy,
    BinDeployPolicy,
    CompilationPolicy,
    CompilationStrategyPolicy,
    CompilationTargetPolicy,
    DependencyPolicy,
    ExecutablesPolicy,
    ManifestPolicy,
    McpPolicy,
    McpTransportPolicy,
    PolicyCache,
    RegistrySourcePolicy,
    SecurityPolicy,
    UnmanagedFilesPolicy,
)


class TestEnforcementEscalation(unittest.TestCase):
    """Enforcement can only escalate: off < warn < block."""

    def _merge_enforcement(self, parent_enf: str, child_enf: str) -> str:
        result = merge_policies(
            ApmPolicy(enforcement=parent_enf),
            ApmPolicy(enforcement=child_enf),
        )
        return result.enforcement

    def test_warn_to_block(self):
        self.assertEqual(self._merge_enforcement("warn", "block"), "block")

    def test_block_cannot_downgrade_to_warn(self):
        self.assertEqual(self._merge_enforcement("block", "warn"), "block")

    def test_off_to_warn(self):
        self.assertEqual(self._merge_enforcement("off", "warn"), "warn")

    def test_block_cannot_downgrade_to_off(self):
        self.assertEqual(self._merge_enforcement("block", "off"), "block")

    def test_same_level(self):
        self.assertEqual(self._merge_enforcement("warn", "warn"), "warn")


class TestCacheMerge(unittest.TestCase):
    """Cache TTL: child can lower, never raise above parent."""

    def test_child_tightens(self):
        result = merge_policies(
            ApmPolicy(cache=PolicyCache(ttl=3600)),
            ApmPolicy(cache=PolicyCache(ttl=1800)),
        )
        self.assertEqual(result.cache.ttl, 1800)

    def test_child_cannot_raise(self):
        result = merge_policies(
            ApmPolicy(cache=PolicyCache(ttl=1800)),
            ApmPolicy(cache=PolicyCache(ttl=3600)),
        )
        self.assertEqual(result.cache.ttl, 1800)

    def test_equal_ttl(self):
        result = merge_policies(
            ApmPolicy(cache=PolicyCache(ttl=900)),
            ApmPolicy(cache=PolicyCache(ttl=900)),
        )
        self.assertEqual(result.cache.ttl, 900)


class TestDependencyDenyMerge(unittest.TestCase):
    """Deny lists: union (child adds, never removes)."""

    def test_union(self):
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(deny=["a/*"])),
            ApmPolicy(dependencies=DependencyPolicy(deny=["b/*"])),
        )
        self.assertEqual(sorted(result.dependencies.deny), ["a/*", "b/*"])

    def test_deduplication(self):
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(deny=["a/*"])),
            ApmPolicy(dependencies=DependencyPolicy(deny=["a/*"])),
        )
        self.assertEqual(result.dependencies.deny, ("a/*",))

    def test_empty_parent(self):
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(deny=[])),
            ApmPolicy(dependencies=DependencyPolicy(deny=["x/*"])),
        )
        self.assertEqual(result.dependencies.deny, ("x/*",))


class TestDependencyAllowMerge(unittest.TestCase):
    """Allow lists: intersection — child can narrow, never widen."""

    def test_intersection(self):
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(allow=["contoso/*", "microsoft/*"])),
            ApmPolicy(dependencies=DependencyPolicy(allow=["contoso/*"])),
        )
        self.assertEqual(result.dependencies.allow, ("contoso/*",))

    def test_parent_empty_child_adds(self):
        """Parent empty (deny-only mode) -> child can introduce allow-list."""
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(allow=[])),
            ApmPolicy(dependencies=DependencyPolicy(allow=["contoso/*"])),
        )
        self.assertEqual(result.dependencies.allow, ())

    def test_child_narrows_to_nothing(self):
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(allow=["contoso/*"])),
            ApmPolicy(dependencies=DependencyPolicy(allow=[])),
        )
        self.assertEqual(result.dependencies.allow, ())

    def test_both_empty(self):
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(allow=[])),
            ApmPolicy(dependencies=DependencyPolicy(allow=[])),
        )
        self.assertEqual(result.dependencies.allow, ())


class TestDependencyRequireMerge(unittest.TestCase):
    """Require lists: union (child adds requirements, never removes)."""

    def test_union(self):
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(require=["contoso/hooks"])),
            ApmPolicy(dependencies=DependencyPolicy(require=["contoso/standards"])),
        )
        self.assertEqual(
            sorted(result.dependencies.require),
            ["contoso/hooks", "contoso/standards"],
        )

    def test_deduplication(self):
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(require=["contoso/hooks"])),
            ApmPolicy(dependencies=DependencyPolicy(require=["contoso/hooks"])),
        )
        self.assertEqual(result.dependencies.require, ("contoso/hooks",))


class TestDependencyTransparency(unittest.TestCase):
    """Child omitting dependencies block is transparent for deny/require (fixes #1201)."""

    def test_parent_require_child_omits_deps_block(self):
        """Parent require + child omits dependencies entirely -> require flows through."""
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(require=("contoso/hooks",))),
            ApmPolicy(),  # child omits dependencies -> require=None
        )
        self.assertEqual(result.dependencies.require, ("contoso/hooks",))

    def test_parent_deny_child_omits_deps_block(self):
        """Parent deny + child omits dependencies entirely -> deny flows through."""
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(deny=("evil/*",))),
            ApmPolicy(),  # child omits dependencies -> deny=None
        )
        self.assertEqual(result.dependencies.deny, ("evil/*",))

    def test_parent_require_child_explicit_empty_require(self):
        """Child explicit empty require=() overrides parent (AC#2)."""
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(require=("contoso/hooks",))),
            ApmPolicy(dependencies=DependencyPolicy(require=())),
        )
        self.assertEqual(result.dependencies.require, ())

    def test_parent_deny_child_explicit_empty_deny(self):
        """Child explicit empty deny=() overrides parent."""
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(deny=("evil/*",))),
            ApmPolicy(dependencies=DependencyPolicy(deny=())),
        )
        self.assertEqual(result.dependencies.deny, ())

    def test_three_level_chain_require_transparency(self):
        """Enterprise require -> org omits -> repo omits -> require preserved."""
        result = resolve_policy_chain(
            [
                ApmPolicy(dependencies=DependencyPolicy(require=("contoso/core",))),
                ApmPolicy(),  # org omits
                ApmPolicy(),  # repo omits
            ]
        )
        self.assertEqual(result.dependencies.require, ("contoso/core",))

    def test_three_level_chain_deny_transparency(self):
        """Enterprise deny -> org omits -> repo omits -> deny preserved."""
        result = resolve_policy_chain(
            [
                ApmPolicy(dependencies=DependencyPolicy(deny=("banned/*",))),
                ApmPolicy(),  # org omits
                ApmPolicy(),  # repo omits
            ]
        )
        self.assertEqual(result.dependencies.deny, ("banned/*",))

    def test_both_none_merged_none(self):
        """Both parent and child omit dependencies -> None (no opinion)."""
        result = merge_policies(ApmPolicy(), ApmPolicy())
        self.assertIsNone(result.dependencies.deny)
        self.assertIsNone(result.dependencies.require)
        self.assertEqual(result.dependencies.effective_deny, ())
        self.assertEqual(result.dependencies.effective_require, ())


class TestRequireResolutionEscalation(unittest.TestCase):
    """require_resolution: project-wins < policy-wins < block."""

    def _merge_resolution(self, parent: str, child: str) -> str:
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(require_resolution=parent)),
            ApmPolicy(dependencies=DependencyPolicy(require_resolution=child)),
        )
        return result.dependencies.require_resolution

    def test_escalate_to_policy_wins(self):
        self.assertEqual(self._merge_resolution("project-wins", "policy-wins"), "policy-wins")

    def test_cannot_downgrade_from_block(self):
        self.assertEqual(self._merge_resolution("block", "project-wins"), "block")

    def test_same_level(self):
        self.assertEqual(self._merge_resolution("policy-wins", "policy-wins"), "policy-wins")


class TestMaxDepthMerge(unittest.TestCase):
    """max_depth: min(parent, child)."""

    def test_child_tightens(self):
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(max_depth=10)),
            ApmPolicy(dependencies=DependencyPolicy(max_depth=5)),
        )
        self.assertEqual(result.dependencies.max_depth, 5)

    def test_child_cannot_raise(self):
        result = merge_policies(
            ApmPolicy(dependencies=DependencyPolicy(max_depth=5)),
            ApmPolicy(dependencies=DependencyPolicy(max_depth=10)),
        )
        self.assertEqual(result.dependencies.max_depth, 5)


class TestMcpMerge(unittest.TestCase):
    """MCP: deny=union, allow=intersection, transport, escalation."""

    def test_deny_union(self):
        result = merge_policies(
            ApmPolicy(mcp=McpPolicy(deny=["evil/*"])),
            ApmPolicy(mcp=McpPolicy(deny=["bad/*"])),
        )
        self.assertEqual(sorted(result.mcp.deny), ["bad/*", "evil/*"])

    def test_allow_intersection(self):
        result = merge_policies(
            ApmPolicy(mcp=McpPolicy(allow=["good/*", "ok/*"])),
            ApmPolicy(mcp=McpPolicy(allow=["good/*"])),
        )
        self.assertEqual(result.mcp.allow, ("good/*",))

    def test_transport_allow_intersection(self):
        result = merge_policies(
            ApmPolicy(mcp=McpPolicy(transport=McpTransportPolicy(allow=["stdio", "sse"]))),
            ApmPolicy(mcp=McpPolicy(transport=McpTransportPolicy(allow=["stdio"]))),
        )
        self.assertEqual(result.mcp.transport.allow, ("stdio",))

    def test_self_defined_escalation(self):
        result = merge_policies(
            ApmPolicy(mcp=McpPolicy(self_defined="allow")),
            ApmPolicy(mcp=McpPolicy(self_defined="warn")),
        )
        self.assertEqual(result.mcp.self_defined, "warn")

    def test_self_defined_cannot_downgrade(self):
        result = merge_policies(
            ApmPolicy(mcp=McpPolicy(self_defined="deny")),
            ApmPolicy(mcp=McpPolicy(self_defined="allow")),
        )
        self.assertEqual(result.mcp.self_defined, "deny")

    def test_trust_transitive_true_to_false(self):
        result = merge_policies(
            ApmPolicy(mcp=McpPolicy(trust_transitive=True)),
            ApmPolicy(mcp=McpPolicy(trust_transitive=False)),
        )
        self.assertFalse(result.mcp.trust_transitive)

    def test_trust_transitive_false_stays_false(self):
        result = merge_policies(
            ApmPolicy(mcp=McpPolicy(trust_transitive=False)),
            ApmPolicy(mcp=McpPolicy(trust_transitive=True)),
        )
        self.assertFalse(result.mcp.trust_transitive)

    def test_trust_transitive_both_true(self):
        result = merge_policies(
            ApmPolicy(mcp=McpPolicy(trust_transitive=True)),
            ApmPolicy(mcp=McpPolicy(trust_transitive=True)),
        )
        self.assertTrue(result.mcp.trust_transitive)


class TestCompilationMerge(unittest.TestCase):
    """Compilation: attribution sticky, enforce parent-wins, allow intersection."""

    def test_source_attribution_parent_true(self):
        result = merge_policies(
            ApmPolicy(compilation=CompilationPolicy(source_attribution=True)),
            ApmPolicy(compilation=CompilationPolicy(source_attribution=False)),
        )
        self.assertTrue(result.compilation.source_attribution)

    def test_source_attribution_child_true(self):
        result = merge_policies(
            ApmPolicy(compilation=CompilationPolicy(source_attribution=False)),
            ApmPolicy(compilation=CompilationPolicy(source_attribution=True)),
        )
        self.assertTrue(result.compilation.source_attribution)

    def test_target_enforce_parent_wins(self):
        result = merge_policies(
            ApmPolicy(
                compilation=CompilationPolicy(target=CompilationTargetPolicy(enforce="vscode"))
            ),
            ApmPolicy(
                compilation=CompilationPolicy(target=CompilationTargetPolicy(enforce="claude"))
            ),
        )
        self.assertEqual(result.compilation.target.enforce, "vscode")

    def test_target_enforce_child_sets_if_parent_unset(self):
        result = merge_policies(
            ApmPolicy(compilation=CompilationPolicy(target=CompilationTargetPolicy(enforce=None))),
            ApmPolicy(
                compilation=CompilationPolicy(target=CompilationTargetPolicy(enforce="claude"))
            ),
        )
        self.assertEqual(result.compilation.target.enforce, "claude")

    def test_target_allow_intersection(self):
        result = merge_policies(
            ApmPolicy(
                compilation=CompilationPolicy(
                    target=CompilationTargetPolicy(allow=["vscode", "claude"])
                )
            ),
            ApmPolicy(
                compilation=CompilationPolicy(target=CompilationTargetPolicy(allow=["vscode"]))
            ),
        )
        self.assertEqual(result.compilation.target.allow, ("vscode",))

    def test_strategy_enforce_parent_wins(self):
        result = merge_policies(
            ApmPolicy(
                compilation=CompilationPolicy(
                    strategy=CompilationStrategyPolicy(enforce="distributed")
                )
            ),
            ApmPolicy(
                compilation=CompilationPolicy(
                    strategy=CompilationStrategyPolicy(enforce="single-file")
                )
            ),
        )
        self.assertEqual(result.compilation.strategy.enforce, "distributed")

    def test_strategy_enforce_child_sets_if_parent_unset(self):
        result = merge_policies(
            ApmPolicy(
                compilation=CompilationPolicy(strategy=CompilationStrategyPolicy(enforce=None))
            ),
            ApmPolicy(
                compilation=CompilationPolicy(
                    strategy=CompilationStrategyPolicy(enforce="single-file")
                )
            ),
        )
        self.assertEqual(result.compilation.strategy.enforce, "single-file")


class TestManifestMerge(unittest.TestCase):
    """Manifest: required_fields union, scripts escalation, content_types intersection."""

    def test_required_fields_union(self):
        result = merge_policies(
            ApmPolicy(manifest=ManifestPolicy(required_fields=["name"])),
            ApmPolicy(manifest=ManifestPolicy(required_fields=["version"])),
        )
        self.assertEqual(sorted(result.manifest.required_fields), ["name", "version"])

    def test_required_fields_dedup(self):
        result = merge_policies(
            ApmPolicy(manifest=ManifestPolicy(required_fields=["name"])),
            ApmPolicy(manifest=ManifestPolicy(required_fields=["name"])),
        )
        self.assertEqual(result.manifest.required_fields, ("name",))

    def test_scripts_escalation(self):
        result = merge_policies(
            ApmPolicy(manifest=ManifestPolicy(scripts="allow")),
            ApmPolicy(manifest=ManifestPolicy(scripts="deny")),
        )
        self.assertEqual(result.manifest.scripts, "deny")

    def test_scripts_cannot_downgrade(self):
        result = merge_policies(
            ApmPolicy(manifest=ManifestPolicy(scripts="deny")),
            ApmPolicy(manifest=ManifestPolicy(scripts="allow")),
        )
        self.assertEqual(result.manifest.scripts, "deny")

    def test_content_types_allow_intersection(self):
        result = merge_policies(
            ApmPolicy(manifest=ManifestPolicy(content_types={"allow": ["prompts", "rules"]})),
            ApmPolicy(manifest=ManifestPolicy(content_types={"allow": ["prompts"]})),
        )
        self.assertEqual(result.manifest.content_types, {"allow": ["prompts"]})

    def test_content_types_none_both(self):
        result = merge_policies(
            ApmPolicy(manifest=ManifestPolicy(content_types=None)),
            ApmPolicy(manifest=ManifestPolicy(content_types=None)),
        )
        self.assertIsNone(result.manifest.content_types)

    def test_content_types_parent_none_child_sets(self):
        result = merge_policies(
            ApmPolicy(manifest=ManifestPolicy(content_types=None)),
            ApmPolicy(manifest=ManifestPolicy(content_types={"allow": ["prompts"]})),
        )
        self.assertEqual(result.manifest.content_types, {"allow": ["prompts"]})


class TestUnmanagedFilesMerge(unittest.TestCase):
    """Unmanaged files: action escalation, directories union."""

    def test_action_escalation_ignore_to_warn(self):
        result = merge_policies(
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="ignore")),
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="warn")),
        )
        self.assertEqual(result.unmanaged_files.action, "warn")

    def test_action_escalation_warn_to_deny(self):
        result = merge_policies(
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="warn")),
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="deny")),
        )
        self.assertEqual(result.unmanaged_files.action, "deny")

    def test_action_cannot_downgrade(self):
        result = merge_policies(
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="deny")),
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="ignore")),
        )
        self.assertEqual(result.unmanaged_files.action, "deny")

    def test_directories_union(self):
        result = merge_policies(
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(directories=[".prompts"])),
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(directories=[".rules"])),
        )
        self.assertEqual(sorted(result.unmanaged_files.directories), [".prompts", ".rules"])

    def test_directories_dedup(self):
        result = merge_policies(
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(directories=[".prompts"])),
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(directories=[".prompts"])),
        )
        self.assertEqual(result.unmanaged_files.directories, (".prompts",))

    def test_child_omitting_unmanaged_files_inherits_parent_issue_1198(self):
        """extends: child without unmanaged_files must not downgrade org deny."""
        parent = ApmPolicy(
            unmanaged_files=UnmanagedFilesPolicy(
                action="deny",
                directories=(
                    ".github/instructions",
                    ".github/agents",
                    ".github/hooks",
                ),
            ),
        )
        child = ApmPolicy(
            dependencies=DependencyPolicy(deny=("**/some-pattern",)),
            unmanaged_files=UnmanagedFilesPolicy(action=None, directories=None),
        )
        result = merge_policies(parent, child)
        self.assertEqual(result.unmanaged_files.action, "deny")
        self.assertEqual(
            result.unmanaged_files.directories,
            (
                ".github/instructions",
                ".github/agents",
                ".github/hooks",
            ),
        )


class TestUnmanagedFilesTransparency(unittest.TestCase):
    """Child omitting unmanaged_files is transparent (fixes #1198)."""

    def test_parent_deny_child_omits_block(self):
        """Parent deny + child omits -> merged deny."""
        result = merge_policies(
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="deny")),
            ApmPolicy(),  # child omits unmanaged_files entirely -> action=None
        )
        self.assertEqual(result.unmanaged_files.action, "deny")

    def test_parent_deny_child_explicit_ignore(self):
        """Parent deny + child explicitly sets ignore -> merged deny (escalation)."""
        result = merge_policies(
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="deny")),
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="ignore")),
        )
        self.assertEqual(result.unmanaged_files.action, "deny")

    def test_parent_warn_child_explicit_deny(self):
        """Parent warn + child explicitly sets deny -> merged deny (child tightens)."""
        result = merge_policies(
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="warn")),
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="deny")),
        )
        self.assertEqual(result.unmanaged_files.action, "deny")

    def test_parent_ignore_child_omits(self):
        """Parent ignore + child omits -> merged ignore."""
        result = merge_policies(
            ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="ignore")),
            ApmPolicy(),
        )
        self.assertEqual(result.unmanaged_files.action, "ignore")

    def test_parent_none_child_omits(self):
        """Both omit -> merged None (no opinion)."""
        result = merge_policies(
            ApmPolicy(),
            ApmPolicy(),
        )
        self.assertIsNone(result.unmanaged_files.action)
        self.assertEqual(result.unmanaged_files.effective_action, "ignore")

    def test_directories_inherited_when_child_omits(self):
        """Parent directories preserved when child omits the block."""
        result = merge_policies(
            ApmPolicy(
                unmanaged_files=UnmanagedFilesPolicy(action="deny", directories=(".github", "docs"))
            ),
            ApmPolicy(),  # child omits
        )
        self.assertEqual(result.unmanaged_files.action, "deny")
        self.assertEqual(sorted(result.unmanaged_files.directories), [".github", "docs"])

    def test_three_level_chain_transparency(self):
        """Enterprise deny -> org omits -> repo omits -> deny preserved."""
        result = resolve_policy_chain(
            [
                ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="deny")),
                ApmPolicy(),  # org omits
                ApmPolicy(),  # repo omits
            ]
        )
        self.assertEqual(result.unmanaged_files.action, "deny")


class TestResolvePolicyChain(unittest.TestCase):
    """Full chain resolution with three levels."""

    def test_three_level_chain(self):
        enterprise = ApmPolicy(
            name="enterprise",
            enforcement="warn",
            dependencies=DependencyPolicy(
                deny=["evil/*"],
                allow=["contoso/*", "microsoft/*"],
            ),
        )
        org = ApmPolicy(
            name="org",
            enforcement="block",
            dependencies=DependencyPolicy(
                deny=["sketchy/*"],
                allow=["contoso/*"],
            ),
        )
        repo = ApmPolicy(
            name="repo",
            enforcement="warn",  # can't downgrade
            dependencies=DependencyPolicy(
                deny=["extra/*"],
                allow=["contoso/*"],
            ),
        )

        result = resolve_policy_chain([enterprise, org, repo])

        self.assertEqual(result.enforcement, "block")
        self.assertEqual(sorted(result.dependencies.deny), ["evil/*", "extra/*", "sketchy/*"])
        self.assertEqual(result.dependencies.allow, ("contoso/*",))
        self.assertIsNone(result.extends)

    def test_empty_chain(self):
        result = resolve_policy_chain([])
        self.assertEqual(result, ApmPolicy())

    def test_single_policy(self):
        policy = ApmPolicy(name="solo", enforcement="block")
        result = resolve_policy_chain([policy])
        self.assertEqual(result.enforcement, "block")
        self.assertEqual(result.name, "solo")


class TestChainDepthValidation(unittest.TestCase):
    """Chain depth must not exceed MAX_CHAIN_DEPTH."""

    def test_valid_depth(self):
        validate_chain_depth(["a", "b", "c"])  # no error

    def test_exact_max_depth(self):
        validate_chain_depth(["x"] * MAX_CHAIN_DEPTH)  # no error

    def test_exceeds_max_depth(self):
        with self.assertRaises(PolicyInheritanceError) as ctx:
            validate_chain_depth(["x"] * (MAX_CHAIN_DEPTH + 1))
        self.assertIn(str(MAX_CHAIN_DEPTH), str(ctx.exception))

    def test_chain_depth_in_resolve(self):
        policies = [ApmPolicy(name=f"p{i}") for i in range(MAX_CHAIN_DEPTH + 1)]
        with self.assertRaises(PolicyInheritanceError):
            resolve_policy_chain(policies)


class TestCycleDetection(unittest.TestCase):
    """Cycle detection helper."""

    def test_cycle_detected(self):
        self.assertTrue(detect_cycle(["a", "b", "c"], "a"))

    def test_no_cycle(self):
        self.assertFalse(detect_cycle(["a", "b", "c"], "d"))

    def test_empty_visited(self):
        self.assertFalse(detect_cycle([], "a"))


class TestEdgeCases(unittest.TestCase):
    """Edge cases: merging with fully-default policies."""

    def test_merge_with_default_parent(self):
        child = ApmPolicy(
            name="child",
            enforcement="block",
            dependencies=DependencyPolicy(deny=["bad/*"]),
        )
        result = merge_policies(ApmPolicy(), child)
        self.assertEqual(result.enforcement, "block")
        self.assertEqual(result.dependencies.deny, ("bad/*",))
        self.assertEqual(result.name, "child")

    def test_merge_with_default_child(self):
        parent = ApmPolicy(
            name="parent",
            enforcement="block",
            dependencies=DependencyPolicy(deny=["bad/*"]),
        )
        result = merge_policies(parent, ApmPolicy())
        self.assertEqual(result.enforcement, "block")
        self.assertEqual(result.dependencies.deny, ("bad/*",))
        self.assertEqual(result.name, "parent")

    def test_both_defaults(self):
        result = merge_policies(ApmPolicy(), ApmPolicy())
        self.assertEqual(result.enforcement, "warn")
        self.assertEqual(result.cache.ttl, 3600)
        self.assertIsNone(result.dependencies.deny)  # None = no opinion from either side
        self.assertEqual(result.dependencies.effective_deny, ())
        self.assertIsNone(result.dependencies.allow)

    def test_extends_cleared_after_merge(self):
        result = merge_policies(
            ApmPolicy(extends="contoso/policy-hub"),
            ApmPolicy(extends="org"),
        )
        self.assertIsNone(result.extends)

    def test_name_from_child(self):
        result = merge_policies(ApmPolicy(name="parent"), ApmPolicy(name="child"))
        self.assertEqual(result.name, "child")

    def test_name_fallback_to_parent(self):
        result = merge_policies(ApmPolicy(name="parent"), ApmPolicy(name=""))
        self.assertEqual(result.name, "parent")


class TestSecurityAuditMerge(unittest.TestCase):
    """security.audit merges as a floor: tightens, never relaxes."""

    @staticmethod
    def _p(on_install=None, external=None):
        return ApmPolicy(
            security=SecurityPolicy(audit=AuditPolicy(on_install=on_install, external=external))
        )

    def test_parent_floor_holds_over_weaker_child(self):
        result = merge_policies(self._p("block"), self._p("warn"))
        self.assertEqual(result.security.audit.on_install, "block")

    def test_child_can_tighten_over_parent(self):
        result = merge_policies(self._p("warn"), self._p("block"))
        self.assertEqual(result.security.audit.on_install, "block")

    def test_none_parent_transparency(self):
        result = merge_policies(self._p(None), self._p("warn"))
        self.assertEqual(result.security.audit.on_install, "warn")

    def test_none_child_transparency(self):
        result = merge_policies(self._p("block"), self._p(None))
        self.assertEqual(result.security.audit.on_install, "block")

    def test_external_union_merged(self):
        result = merge_policies(self._p("block", ("skillspector",)), self._p("block", ("sarif",)))
        self.assertEqual(set(result.security.audit.external), {"skillspector", "sarif"})


class TestFetchFailureEscalation(unittest.TestCase):
    """fetch_failure can only escalate: warn < block (closes #829 inheritance)."""

    def _merge(self, parent_val: str, child_val: str) -> str:
        return merge_policies(
            ApmPolicy(fetch_failure=parent_val),
            ApmPolicy(fetch_failure=child_val),
        ).fetch_failure

    def test_parent_block_not_relaxed_by_silent_child(self):
        # Child default is "warn"; it must not relax a parent "block".
        self.assertEqual(self._merge("block", "warn"), "block")

    def test_child_can_tighten(self):
        self.assertEqual(self._merge("warn", "block"), "block")

    def test_same_level(self):
        self.assertEqual(self._merge("warn", "warn"), "warn")


class TestRegistrySourceMerge(unittest.TestCase):
    """registry_source tightens: require unions, allow_non_registry restricts."""

    def test_parent_require_preserved_when_child_silent(self):
        parent = ApmPolicy(registry_source=RegistrySourcePolicy(require=("corp",)))
        result = merge_policies(parent, ApmPolicy())
        self.assertEqual(result.registry_source.require, ("corp",))

    def test_require_union_merged(self):
        parent = ApmPolicy(registry_source=RegistrySourcePolicy(require=("corp",)))
        child = ApmPolicy(registry_source=RegistrySourcePolicy(require=("mirror",)))
        result = merge_policies(parent, child)
        self.assertEqual(set(result.registry_source.require), {"corp", "mirror"})

    def test_allow_non_registry_parent_false_preserved(self):
        parent = ApmPolicy(registry_source=RegistrySourcePolicy(allow_non_registry=False))
        result = merge_policies(parent, ApmPolicy())
        self.assertFalse(result.registry_source.allow_non_registry)

    def test_allow_non_registry_child_can_tighten(self):
        parent = ApmPolicy(registry_source=RegistrySourcePolicy(allow_non_registry=True))
        child = ApmPolicy(registry_source=RegistrySourcePolicy(allow_non_registry=False))
        result = merge_policies(parent, child)
        self.assertFalse(result.registry_source.allow_non_registry)

    def test_allow_non_registry_child_cannot_relax(self):
        parent = ApmPolicy(registry_source=RegistrySourcePolicy(allow_non_registry=False))
        child = ApmPolicy(registry_source=RegistrySourcePolicy(allow_non_registry=True))
        result = merge_policies(parent, child)
        self.assertFalse(result.registry_source.allow_non_registry)


class TestBinDeployMerge(unittest.TestCase):
    """bin_deploy tightens: deny_all sticks True, deny unions."""

    def test_parent_deny_all_preserved_when_child_silent(self):
        parent = ApmPolicy(bin_deploy=BinDeployPolicy(deny_all=True))
        result = merge_policies(parent, ApmPolicy())
        self.assertTrue(result.bin_deploy.deny_all)

    def test_deny_all_child_can_tighten(self):
        parent = ApmPolicy(bin_deploy=BinDeployPolicy(deny_all=False))
        child = ApmPolicy(bin_deploy=BinDeployPolicy(deny_all=True))
        result = merge_policies(parent, child)
        self.assertTrue(result.bin_deploy.deny_all)

    def test_deny_all_child_cannot_relax(self):
        parent = ApmPolicy(bin_deploy=BinDeployPolicy(deny_all=True))
        child = ApmPolicy(bin_deploy=BinDeployPolicy(deny_all=False))
        result = merge_policies(parent, child)
        self.assertTrue(result.bin_deploy.deny_all)

    def test_parent_deny_preserved_when_child_silent(self):
        parent = ApmPolicy(bin_deploy=BinDeployPolicy(deny=("owner/repo",)))
        result = merge_policies(parent, ApmPolicy())
        self.assertEqual(result.bin_deploy.deny, ("owner/repo",))

    def test_deny_union_merged(self):
        parent = ApmPolicy(bin_deploy=BinDeployPolicy(deny=("a/b",)))
        child = ApmPolicy(bin_deploy=BinDeployPolicy(deny=("c/d",)))
        result = merge_policies(parent, child)
        self.assertEqual(set(result.bin_deploy.deny), {"a/b", "c/d"})


class TestExecutablesMerge(unittest.TestCase):
    """executables tightens: deny_all OR-sticks; lists union (issue #1873)."""

    def test_deny_all_sticks_when_child_silent(self):
        parent = ApmPolicy(executables=ExecutablesPolicy(deny_all=True))
        result = merge_policies(parent, ApmPolicy())
        self.assertTrue(result.executables.deny_all)

    def test_deny_all_child_cannot_relax(self):
        parent = ApmPolicy(executables=ExecutablesPolicy(deny_all=True))
        child = ApmPolicy(executables=ExecutablesPolicy(deny_all=False))
        result = merge_policies(parent, child)
        self.assertTrue(result.executables.deny_all)

    def test_deny_union_merged(self):
        parent = ApmPolicy(executables=ExecutablesPolicy(deny=("a/b",)))
        child = ApmPolicy(executables=ExecutablesPolicy(deny=("c/d",)))
        result = merge_policies(parent, child)
        self.assertEqual(set(result.executables.deny), {"a/b", "c/d"})

    def test_require_union_merged(self):
        parent = ApmPolicy(executables=ExecutablesPolicy(require=("org/hook",)))
        child = ApmPolicy(executables=ExecutablesPolicy(require=("org/mcp",)))
        result = merge_policies(parent, child)
        self.assertEqual(set(result.executables.require), {"org/hook", "org/mcp"})

    def test_recommend_union_merged(self):
        parent = ApmPolicy(executables=ExecutablesPolicy(recommend=("org/a",)))
        child = ApmPolicy(executables=ExecutablesPolicy(recommend=("org/b",)))
        result = merge_policies(parent, child)
        self.assertEqual(set(result.executables.recommend), {"org/a", "org/b"})

    def test_enforce_union_merged(self):
        parent = ApmPolicy(executables=ExecutablesPolicy(enforce=("org/x",)))
        result = merge_policies(parent, ApmPolicy())
        self.assertEqual(result.executables.enforce, ("org/x",))


class TestMergeUnmanagedExclude(unittest.TestCase):
    """The unmanaged-files ``exclude`` list is union-merged across a chain."""

    def test_child_exclude_unions_with_parent(self):
        parent = ApmPolicy(
            unmanaged_files=UnmanagedFilesPolicy(action="deny", exclude=(".github/copilot",))
        )
        child = ApmPolicy(
            unmanaged_files=UnmanagedFilesPolicy(action=None, exclude=(".vscode/mcp.json",))
        )
        merged = merge_policies(parent, child)
        self.assertEqual(
            set(merged.unmanaged_files.exclude),
            {".github/copilot", ".vscode/mcp.json"},
        )

    def test_parent_exclude_inherited_when_child_silent(self):
        parent = ApmPolicy(
            unmanaged_files=UnmanagedFilesPolicy(action="deny", exclude=(".github/copilot",))
        )
        child = ApmPolicy()  # no unmanaged_files block at all
        merged = merge_policies(parent, child)
        self.assertEqual(merged.unmanaged_files.exclude, (".github/copilot",))

    def test_child_only_exclude_is_not_transparent(self):
        # A child that sets only exclude must still carry it through, even
        # though action and directories are None.
        parent = ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="deny"))
        child = ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(exclude=(".cursor/rules/local.md",)))
        merged = merge_policies(parent, child)
        self.assertIn(".cursor/rules/local.md", merged.unmanaged_files.exclude)


class TestMergeFieldCoverageGuard(unittest.TestCase):
    """Regression trap for the whole class of "forgotten field" bugs.

    A field added to ``ApmPolicy`` but not handled in ``merge_policies``
    silently reverts to its default when a policy ``extends`` another.
    This guard builds a parent whose every mergeable field is non-default,
    merges it with a silent (default) child, and asserts no field reverted
    -- so a future forgotten field MUST fail here.
    """

    EXEMPT: ClassVar[set[str]] = {"name", "version", "extends"}

    @staticmethod
    def _non_default_parent() -> ApmPolicy:
        # Each sample value is chosen so that merging with a silent child
        # yields a result distinct from that field's default, proving the
        # parent value was carried through rather than dropped.
        return ApmPolicy(
            enforcement="block",
            fetch_failure="block",
            cache=PolicyCache(ttl=1800),
            dependencies=DependencyPolicy(max_depth=10),
            mcp=McpPolicy(self_defined="deny"),
            compilation=CompilationPolicy(source_attribution=True),
            manifest=ManifestPolicy(scripts="deny"),
            unmanaged_files=UnmanagedFilesPolicy(action="deny"),
            registry_source=RegistrySourcePolicy(require=("corp",)),
            security=SecurityPolicy(audit=AuditPolicy(on_install="block")),
            bin_deploy=BinDeployPolicy(deny_all=True),
            executables=ExecutablesPolicy(deny_all=True),
        )

    def test_sample_covers_every_mergeable_field(self):
        default = ApmPolicy()
        sample = self._non_default_parent()
        sampled = {
            f.name
            for f in dataclasses.fields(ApmPolicy)
            if getattr(sample, f.name) != getattr(default, f.name)
        }
        all_fields = {f.name for f in dataclasses.fields(ApmPolicy)}
        self.assertEqual(sampled, all_fields - self.EXEMPT)

    def test_no_field_dropped_on_merge(self):
        parent = self._non_default_parent()
        merged = merge_policies(parent, ApmPolicy())
        default = ApmPolicy()
        for f in dataclasses.fields(ApmPolicy):
            if f.name in self.EXEMPT:
                continue
            self.assertNotEqual(
                getattr(merged, f.name),
                getattr(default, f.name),
                f"merge_policies dropped field {f.name!r}: it reverted to its default",
            )


if __name__ == "__main__":
    unittest.main()
