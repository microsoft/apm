"""Tests for apm_cli.policy.matcher (pattern matching and allow/deny checks)."""

import unittest

from apm_cli.policy.matcher import (
    check_dependency_allowed,
    check_mcp_allowed,
    matches_pattern,
)
from apm_cli.policy.schema import DependencyPolicy, McpPolicy


class TestMatchesPattern(unittest.TestCase):
    """Test glob-style pattern matching."""

    def test_exact_match(self):
        self.assertTrue(matches_pattern("contoso/repo", "contoso/repo"))

    def test_exact_no_match(self):
        self.assertFalse(matches_pattern("contoso/repo", "contoso/other"))

    def test_single_wildcard_matches_segment(self):
        self.assertTrue(matches_pattern("contoso/foo", "contoso/*"))

    def test_single_wildcard_no_deeper(self):
        self.assertFalse(matches_pattern("contoso/foo/bar", "contoso/*"))

    def test_double_wildcard_matches_deep(self):
        self.assertTrue(matches_pattern("contoso/foo/bar/baz", "contoso/**"))

    def test_double_wildcard_matches_single_level(self):
        self.assertTrue(matches_pattern("contoso/foo", "contoso/**"))

    def test_host_qualified(self):
        self.assertTrue(matches_pattern("gitlab.com/org/repo", "gitlab.com/**"))

    def test_host_qualified_single_star_no_match(self):
        self.assertFalse(matches_pattern("gitlab.com/org/repo", "gitlab.com/*"))

    def test_host_qualified_single_star_match(self):
        self.assertTrue(matches_pattern("gitlab.com/org", "gitlab.com/*"))

    def test_empty_pattern_no_match(self):
        self.assertFalse(matches_pattern("contoso/repo", ""))

    def test_empty_ref_no_match(self):
        self.assertFalse(matches_pattern("", "contoso/*"))

    def test_both_empty_no_match(self):
        self.assertFalse(matches_pattern("", ""))

    def test_wildcard_in_middle(self):
        self.assertTrue(matches_pattern("contoso/foo/bar", "contoso/*/bar"))

    def test_wildcard_in_middle_no_match(self):
        self.assertFalse(matches_pattern("contoso/foo/baz/bar", "contoso/*/bar"))

    def test_double_wildcard_in_middle(self):
        self.assertTrue(matches_pattern("contoso/a/b/c/bar", "contoso/**/bar"))


class TestCheckDependencyAllowed(unittest.TestCase):
    """Test dependency allow/deny logic."""

    def test_deny_wins_over_allow(self):
        policy = DependencyPolicy(
            allow=["contoso/*"],
            deny=["contoso/evil"],
        )
        allowed, reason = check_dependency_allowed("contoso/evil", policy)
        self.assertFalse(allowed)
        self.assertIn("denied by pattern", reason)

    def test_empty_allow_is_deny_only(self):
        policy = DependencyPolicy(deny=["evil-corp/**"])
        allowed, reason = check_dependency_allowed("contoso/good", policy)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_empty_allow_deny_matches(self):
        policy = DependencyPolicy(deny=["evil-corp/**"])
        allowed, reason = check_dependency_allowed("evil-corp/bad", policy)  # noqa: RUF059
        self.assertFalse(allowed)

    def test_allowlist_mode_ref_allowed(self):
        policy = DependencyPolicy(allow=["contoso/*"])
        allowed, reason = check_dependency_allowed("contoso/lib", policy)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_allowlist_mode_ref_blocked(self):
        policy = DependencyPolicy(allow=["contoso/*"])
        allowed, reason = check_dependency_allowed("other/lib", policy)
        self.assertFalse(allowed)
        self.assertIn("not in allowed sources", reason)

    def test_no_rules_everything_allowed(self):
        policy = DependencyPolicy()
        allowed, reason = check_dependency_allowed("anything/here", policy)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_deny_pattern_with_double_wildcard(self):
        policy = DependencyPolicy(deny=["untrusted/**"])
        allowed, reason = check_dependency_allowed("untrusted/deep/nested", policy)  # noqa: RUF059
        self.assertFalse(allowed)


class TestCheckMcpAllowed(unittest.TestCase):
    """Test MCP server allow/deny logic."""

    def test_deny_wins(self):
        policy = McpPolicy(
            allow=["trusted/*"],
            deny=["trusted/bad"],
        )
        allowed, reason = check_mcp_allowed("trusted/bad", policy)
        self.assertFalse(allowed)
        self.assertIn("denied", reason)

    def test_empty_allow_deny_only(self):
        policy = McpPolicy(deny=["bad-server"])
        allowed, reason = check_mcp_allowed("good-server", policy)  # noqa: RUF059
        self.assertTrue(allowed)

    def test_allowlist_blocks_unlisted(self):
        policy = McpPolicy(allow=["approved/*"])
        allowed, reason = check_mcp_allowed("rogue-server", policy)
        self.assertFalse(allowed)
        self.assertIn("not in allowed sources", reason)

    def test_allowlist_permits_match(self):
        policy = McpPolicy(allow=["approved/*"])
        allowed, reason = check_mcp_allowed("approved/tool", policy)  # noqa: RUF059
        self.assertTrue(allowed)

    def test_no_rules_all_allowed(self):
        policy = McpPolicy()
        allowed, reason = check_mcp_allowed("anything", policy)  # noqa: RUF059
        self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()
