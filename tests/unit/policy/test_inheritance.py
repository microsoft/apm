"""Tests for policy inheritance chain resolution and merge logic."""

from __future__ import annotations

import unittest

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
    CompilationPolicy,
    CompilationStrategyPolicy,
    CompilationTargetPolicy,
    DependencyPolicy,
    ManifestPolicy,
    McpPolicy,
    McpTransportPolicy,
    PolicyCache,
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
        self.assertEqual(result.dependencies.deny, ())
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


if __name__ == "__main__":
    unittest.main()
