"""Verify all policy fixture files parse correctly.

Quick smoke test that ensures every YAML fixture under tests/fixtures/policy/
is a valid apm-policy.yml that the parser accepts.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from apm_cli.policy.parser import load_policy

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "policy"


class TestPolicyFixtures(unittest.TestCase):
    """Verify all policy fixture files are valid."""

    def test_all_fixtures_parse(self):
        yml_files = sorted(FIXTURES_DIR.glob("*.yml"))
        self.assertTrue(len(yml_files) > 0, "No fixture files found")

        for yml_file in yml_files:
            with self.subTest(fixture=yml_file.name):
                policy, warnings = load_policy(yml_file)  # noqa: RUF059
                self.assertIsNotNone(policy)
                self.assertTrue(policy.name)

    def test_org_policy_fields(self):
        """Org policy fixture contains expected field values."""
        policy, warnings = load_policy(FIXTURES_DIR / "org-policy.yml")  # noqa: RUF059
        self.assertEqual(policy.name, "devexpgbb-test-policy")
        self.assertEqual(policy.enforcement, "warn")
        self.assertIn("DevExpGbb/*", policy.dependencies.allow)
        self.assertIn("test-blocked/*", policy.dependencies.deny)
        self.assertEqual(policy.dependencies.require_resolution, "project-wins")
        self.assertEqual(policy.cache.ttl, 3600)
        self.assertEqual(policy.unmanaged_files.action, "warn")

    def test_enterprise_hub_policy_fields(self):
        """Enterprise hub fixture has strict settings."""
        policy, warnings = load_policy(FIXTURES_DIR / "enterprise-hub-policy.yml")  # noqa: RUF059
        self.assertEqual(policy.enforcement, "block")
        self.assertEqual(policy.dependencies.require_resolution, "policy-wins")
        self.assertIn("contoso-governance/coding-standards", policy.dependencies.require)
        self.assertEqual(policy.unmanaged_files.action, "deny")

    def test_minimal_policy_defaults(self):
        """Minimal fixture fills in defaults correctly."""
        policy, warnings = load_policy(FIXTURES_DIR / "minimal-policy.yml")  # noqa: RUF059
        self.assertEqual(policy.name, "minimal")
        self.assertEqual(policy.enforcement, "warn")
        self.assertEqual(policy.dependencies.require_resolution, "project-wins")
        self.assertEqual(policy.unmanaged_files.action, "ignore")

    def test_repo_override_has_extends(self):
        """Repo override fixture declares extends=org."""
        policy, warnings = load_policy(FIXTURES_DIR / "repo-override-policy.yml")  # noqa: RUF059
        self.assertEqual(policy.extends, "org")
        self.assertIn("experimental/*", policy.dependencies.deny)


if __name__ == "__main__":
    unittest.main()
