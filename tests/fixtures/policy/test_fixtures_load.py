"""W1 fixture validation -- load every policy fixture, verify parse or expected error.

Walks tests/fixtures/policy/ and asserts:
- Valid top-level fixtures load via load_policy() and return an ApmPolicy.
- Chain support files in chains/ all parse individually.
- invalid/apm-policy-malformed.yml raises PolicyValidationError.
- invalid/apm-policy-empty.yml loads to ApmPolicy with defaults (name="").
- Extends-cycle fixtures parse individually (cycle detected at chain resolution).
- Extends-depth fixtures parse individually (depth validated at chain resolution).
- Cycle detection works via detect_cycle().
- Chain depth validation rejects chains exceeding MAX_CHAIN_DEPTH=5.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from apm_cli.policy.inheritance import (
    MAX_CHAIN_DEPTH,
    PolicyInheritanceError,
    detect_cycle,
    resolve_policy_chain,
    validate_chain_depth,  # noqa: F401
)
from apm_cli.policy.parser import PolicyValidationError, load_policy

FIXTURES_DIR = Path(__file__).parent


class TestValidPolicyFixturesParse(unittest.TestCase):
    """Every top-level *.yml fixture must parse without error."""

    def test_all_top_level_fixtures_parse(self):
        """Walk *.yml in fixtures/policy/ (not subdirs) and assert each loads."""
        yml_files = sorted(FIXTURES_DIR.glob("*.yml"))
        self.assertTrue(len(yml_files) >= 14, f"Expected >=14 fixtures, found {len(yml_files)}")

        for yml_file in yml_files:
            with self.subTest(fixture=yml_file.name):
                policy, warnings = load_policy(yml_file)  # noqa: RUF059
                self.assertIsNotNone(policy)
                # All top-level fixtures have a name field
                self.assertTrue(policy.name, f"{yml_file.name}: expected non-empty name")

    def test_chain_support_files_parse(self):
        """Every chains/*.yml must parse without error."""
        chain_files = sorted((FIXTURES_DIR / "chains").glob("*.yml"))
        self.assertTrue(
            len(chain_files) >= 6, f"Expected >=6 chain files, found {len(chain_files)}"
        )

        for yml_file in chain_files:
            with self.subTest(fixture=yml_file.name):
                policy, warnings = load_policy(yml_file)  # noqa: RUF059
                self.assertIsNotNone(policy)


class TestAllowPolicy(unittest.TestCase):
    """Fixture 1: apm-policy-allow.yml"""

    def test_allow_list_values(self):
        policy, _ = load_policy(FIXTURES_DIR / "apm-policy-allow.yml")
        self.assertEqual(policy.enforcement, "warn")
        self.assertIsNotNone(policy.dependencies.allow)
        self.assertIn("DevExpGbb/*", policy.dependencies.allow)
        self.assertIn("microsoft/*", policy.dependencies.allow)


class TestDenyPolicy(unittest.TestCase):
    """Fixture 2: apm-policy-deny.yml"""

    def test_deny_list_values(self):
        policy, _ = load_policy(FIXTURES_DIR / "apm-policy-deny.yml")
        self.assertEqual(policy.enforcement, "block")
        self.assertIn("test-blocked/*", policy.dependencies.deny)


class TestRequiredPolicy(unittest.TestCase):
    """Fixture 3: apm-policy-required.yml"""

    def test_required_deps(self):
        policy, _ = load_policy(FIXTURES_DIR / "apm-policy-required.yml")
        self.assertEqual(policy.enforcement, "block")
        self.assertIn("DevExpGbb/required-standards", policy.dependencies.require)


class TestRequiredVersionPolicy(unittest.TestCase):
    """Fixture 4: apm-policy-required-version.yml"""

    def test_version_pin_and_resolution(self):
        policy, _ = load_policy(FIXTURES_DIR / "apm-policy-required-version.yml")
        self.assertEqual(policy.enforcement, "block")
        self.assertIn("DevExpGbb/required-standards#v2.0.0", policy.dependencies.require)
        self.assertEqual(policy.dependencies.require_resolution, "project-wins")


class TestMalformedPolicy(unittest.TestCase):
    """Fixture 5: invalid/apm-policy-malformed.yml"""

    def test_malformed_raises_validation_error(self):
        malformed = FIXTURES_DIR / "invalid" / "apm-policy-malformed.yml"
        with self.assertRaises(PolicyValidationError) as ctx:
            load_policy(malformed)
        # Error should mention the invalid enforcement value
        self.assertTrue(
            any("enforcement" in e for e in ctx.exception.errors),
            f"Expected enforcement error, got: {ctx.exception.errors}",
        )


class TestWarnPolicy(unittest.TestCase):
    """Fixture 6: apm-policy-warn.yml"""

    def test_warn_enforcement(self):
        policy, _ = load_policy(FIXTURES_DIR / "apm-policy-warn.yml")
        self.assertEqual(policy.enforcement, "warn")


class TestBlockPolicy(unittest.TestCase):
    """Fixture 7: apm-policy-block.yml"""

    def test_block_enforcement(self):
        policy, _ = load_policy(FIXTURES_DIR / "apm-policy-block.yml")
        self.assertEqual(policy.enforcement, "block")


class TestOffPolicy(unittest.TestCase):
    """Fixture 8: apm-policy-off.yml -- YAML boolean coercion test."""

    def test_off_enforcement_via_yaml_coercion(self):
        policy, _ = load_policy(FIXTURES_DIR / "apm-policy-off.yml")
        # YAML 1.1 parses bare `off` as False; parser coerces to "off"
        self.assertEqual(policy.enforcement, "off")


class TestEmptyPolicy(unittest.TestCase):
    """Fixture 9: invalid/apm-policy-empty.yml -- literally {}."""

    def test_empty_loads_to_defaults(self):
        policy, warnings = load_policy(FIXTURES_DIR / "invalid" / "apm-policy-empty.yml")  # noqa: RUF059
        # Empty dict is valid YAML; parser fills all defaults
        self.assertIsNotNone(policy)
        self.assertEqual(policy.name, "")
        self.assertEqual(policy.enforcement, "warn")  # schema default
        self.assertIsNone(policy.dependencies.allow)  # no opinion
        self.assertEqual(policy.dependencies.deny, ())  # empty tuple
        self.assertIsNone(policy.extends)


class TestExtendsCycleFixtures(unittest.TestCase):
    """Fixture 10: apm-policy-extends-cycle.yml + sibling parent."""

    def test_cycle_files_parse_individually(self):
        """Each file in the cycle pair must parse on its own."""
        a, _ = load_policy(FIXTURES_DIR / "apm-policy-extends-cycle.yml")
        b, _ = load_policy(FIXTURES_DIR / "apm-policy-extends-cycle-parent.yml")
        self.assertEqual(a.extends, "cycle-policy-b")
        self.assertEqual(b.extends, "cycle-policy-a")

    def test_detect_cycle_catches_loop(self):
        """detect_cycle() returns True when next_ref is already visited."""
        visited = ["cycle-policy-a", "cycle-policy-b"]
        self.assertTrue(detect_cycle(visited, "cycle-policy-a"))
        self.assertFalse(detect_cycle(visited, "cycle-policy-c"))


class TestExtendsDepthFixtures(unittest.TestCase):
    """Fixture 11: apm-policy-extends-depth.yml + chains/*.yml"""

    def test_chain_files_load_in_order(self):
        """All 6 chain files + leaf parse and form a 7-element chain."""
        chain_dir = FIXTURES_DIR / "chains"
        chain_files = [chain_dir / f"depth-{i}.yml" for i in range(6)]
        leaf_file = FIXTURES_DIR / "apm-policy-extends-depth.yml"

        policies = []
        for f in chain_files + [leaf_file]:  # noqa: RUF005
            policy, _ = load_policy(f)
            policies.append(policy)

        self.assertEqual(len(policies), 7)
        # Root has no extends
        self.assertIsNone(policies[0].extends)
        # Leaf is the last
        self.assertEqual(policies[6].name, "depth-leaf-policy")

    def test_depth_limit_triggers_on_deep_chain(self):
        """resolve_policy_chain rejects a 7-element chain (MAX_CHAIN_DEPTH=5)."""
        chain_dir = FIXTURES_DIR / "chains"
        policies = []
        for i in range(6):
            p, _ = load_policy(chain_dir / f"depth-{i}.yml")
            policies.append(p)
        leaf, _ = load_policy(FIXTURES_DIR / "apm-policy-extends-depth.yml")
        policies.append(leaf)

        self.assertEqual(len(policies), 7)
        self.assertGreater(len(policies), MAX_CHAIN_DEPTH)

        with self.assertRaises(PolicyInheritanceError) as ctx:
            resolve_policy_chain(policies)
        self.assertIn("exceeds maximum", str(ctx.exception))

    def test_depth_limit_allows_valid_chain(self):
        """A chain of exactly MAX_CHAIN_DEPTH policies is accepted."""
        chain_dir = FIXTURES_DIR / "chains"
        policies = []
        for i in range(MAX_CHAIN_DEPTH):
            p, _ = load_policy(chain_dir / f"depth-{i}.yml")
            policies.append(p)

        # Should not raise
        merged = resolve_policy_chain(policies)
        self.assertIsNotNone(merged)


class TestExtends404ParentFixture(unittest.TestCase):
    """Fixture 12: apm-policy-extends-404-parent.yml"""

    def test_parses_with_nonexistent_extends_ref(self):
        """File parses; extends is a string ref to a non-existent parent."""
        policy, _ = load_policy(FIXTURES_DIR / "apm-policy-extends-404-parent.yml")
        self.assertEqual(policy.extends, "nonexistent-org/nonexistent-policy-repo")


class TestMcpPolicy(unittest.TestCase):
    """Fixture 13: apm-policy-mcp.yml"""

    def test_mcp_fields(self):
        policy, _ = load_policy(FIXTURES_DIR / "apm-policy-mcp.yml")
        self.assertEqual(policy.enforcement, "block")
        # allow
        self.assertIn("io.github.github/*", policy.mcp.allow)
        self.assertIn("io.github.modelcontextprotocol/*", policy.mcp.allow)
        # deny
        self.assertIn("io.github.untrusted/*", policy.mcp.deny)
        # transport
        self.assertIsNotNone(policy.mcp.transport.allow)
        self.assertIn("stdio", policy.mcp.transport.allow)
        self.assertIn("http", policy.mcp.transport.allow)
        # self_defined
        self.assertEqual(policy.mcp.self_defined, "warn")
        # trust_transitive
        self.assertFalse(policy.mcp.trust_transitive)


class TestTargetAllowPolicy(unittest.TestCase):
    """Fixture 14: apm-policy-target-allow.yml"""

    def test_target_allow_vscode_only(self):
        policy, _ = load_policy(FIXTURES_DIR / "apm-policy-target-allow.yml")
        self.assertEqual(policy.enforcement, "block")
        self.assertIsNotNone(policy.compilation.target.allow)
        self.assertEqual(policy.compilation.target.allow, ("vscode",))


class TestProjectFixturesExist(unittest.TestCase):
    """Verify all project fixture directories have apm.yml files."""

    EXPECTED_PROJECTS = [  # noqa: RUF012
        "denied-direct",
        "denied-transitive",
        "required-missing",
        "required-version-mismatch",
        "mcp-denied",
        "target-mismatch",
        "unpacked-bundle",
    ]

    def test_all_project_fixtures_have_apm_yml(self):
        projects_dir = FIXTURES_DIR / "projects"
        for project in self.EXPECTED_PROJECTS:
            with self.subTest(project=project):
                apm_yml = projects_dir / project / "apm.yml"
                self.assertTrue(apm_yml.is_file(), f"Missing: {apm_yml}")

    def test_unpacked_bundle_has_no_git_dir(self):
        """unpacked-bundle/ must NOT have a .git/ directory (rubber-duck I5)."""
        bundle_dir = FIXTURES_DIR / "projects" / "unpacked-bundle"
        self.assertFalse(
            (bundle_dir / ".git").exists(),
            "unpacked-bundle/ must not contain .git/ (simulates non-git context)",
        )


if __name__ == "__main__":
    unittest.main()
