"""Tests for apm_cli.policy.schema dataclasses."""

import unittest

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


class TestPolicyCacheDefaults(unittest.TestCase):
    """Test PolicyCache defaults and immutability."""

    def test_default_ttl(self):
        cache = PolicyCache()
        self.assertEqual(cache.ttl, 3600)

    def test_custom_ttl(self):
        cache = PolicyCache(ttl=7200)
        self.assertEqual(cache.ttl, 7200)

    def test_frozen(self):
        cache = PolicyCache()
        with self.assertRaises(AttributeError):
            cache.ttl = 100  # type: ignore[misc]


class TestDependencyPolicyDefaults(unittest.TestCase):
    """Test DependencyPolicy defaults."""

    def test_defaults(self):
        dep = DependencyPolicy()
        self.assertIsNone(dep.allow)
        self.assertIsNone(dep.deny)
        self.assertIsNone(dep.require)
        self.assertEqual(dep.effective_deny, ())
        self.assertEqual(dep.effective_require, ())
        self.assertEqual(dep.require_resolution, "project-wins")
        self.assertEqual(dep.max_depth, 50)

    def test_frozen(self):
        dep = DependencyPolicy()
        with self.assertRaises(AttributeError):
            dep.allow = ["x"]  # type: ignore[misc]


class TestMcpPolicyDefaults(unittest.TestCase):
    """Test McpPolicy defaults."""

    def test_defaults(self):
        mcp = McpPolicy()
        self.assertIsNone(mcp.allow)
        self.assertEqual(mcp.deny, ())
        self.assertIsInstance(mcp.transport, McpTransportPolicy)
        self.assertIsNone(mcp.transport.allow)
        self.assertEqual(mcp.self_defined, "warn")
        self.assertFalse(mcp.trust_transitive)

    def test_frozen(self):
        mcp = McpPolicy()
        with self.assertRaises(AttributeError):
            mcp.self_defined = "deny"  # type: ignore[misc]


class TestCompilationPolicyDefaults(unittest.TestCase):
    """Test CompilationPolicy defaults."""

    def test_defaults(self):
        comp = CompilationPolicy()
        self.assertIsInstance(comp.target, CompilationTargetPolicy)
        self.assertIsInstance(comp.strategy, CompilationStrategyPolicy)
        self.assertFalse(comp.source_attribution)
        self.assertIsNone(comp.target.allow)
        self.assertIsNone(comp.target.enforce)
        self.assertIsNone(comp.strategy.enforce)


class TestManifestPolicyDefaults(unittest.TestCase):
    """Test ManifestPolicy defaults."""

    def test_defaults(self):
        mp = ManifestPolicy()
        self.assertEqual(mp.required_fields, ())
        self.assertEqual(mp.scripts, "allow")
        self.assertIsNone(mp.content_types)


class TestUnmanagedFilesPolicyDefaults(unittest.TestCase):
    """Test UnmanagedFilesPolicy defaults."""

    def test_defaults(self):
        uf = UnmanagedFilesPolicy()
        self.assertIsNone(uf.action)
        self.assertEqual(uf.effective_action, "ignore")
        self.assertEqual(uf.directories, ())


class TestApmPolicyDefaults(unittest.TestCase):
    """Test ApmPolicy top-level defaults and construction."""

    def test_defaults(self):
        policy = ApmPolicy()
        self.assertEqual(policy.name, "")
        self.assertEqual(policy.version, "")
        self.assertIsNone(policy.extends)
        self.assertEqual(policy.enforcement, "warn")
        self.assertIsInstance(policy.cache, PolicyCache)
        self.assertIsInstance(policy.dependencies, DependencyPolicy)
        self.assertIsInstance(policy.mcp, McpPolicy)
        self.assertIsInstance(policy.compilation, CompilationPolicy)
        self.assertIsInstance(policy.manifest, ManifestPolicy)
        self.assertIsInstance(policy.unmanaged_files, UnmanagedFilesPolicy)

    def test_custom_construction(self):
        policy = ApmPolicy(
            name="test-policy",
            version="1.0.0",
            extends="org",
            enforcement="block",
            dependencies=DependencyPolicy(allow=("contoso/*",)),
        )
        self.assertEqual(policy.name, "test-policy")
        self.assertEqual(policy.version, "1.0.0")
        self.assertEqual(policy.extends, "org")
        self.assertEqual(policy.enforcement, "block")
        self.assertEqual(policy.dependencies.allow, ("contoso/*",))

    def test_frozen(self):
        policy = ApmPolicy()
        with self.assertRaises(AttributeError):
            policy.name = "modified"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
