"""End-to-end tests for policy discovery.

These tests verify the full policy discovery pipeline:
- Git remote parsing -> org extraction -> API fetch -> cache -> parse

Gated by: APM_POLICY_E2E_TESTS=1

When running locally without the env var, these tests are SKIPPED.
When running with APM_POLICY_E2E_TESTS=1 but without GITHUB_APM_PAT,
tests that require API access are skipped.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

POLICY_E2E = os.environ.get("APM_POLICY_E2E_TESTS") == "1"
HAS_TOKEN = bool(os.environ.get("GITHUB_APM_PAT") or os.environ.get("GITHUB_TOKEN"))

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "policy"


@unittest.skipUnless(POLICY_E2E, "APM_POLICY_E2E_TESTS not set")
class TestPolicyDiscoveryE2E(unittest.TestCase):
    """E2E tests for the policy discovery pipeline."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -- Local file discovery ---------------------------------------

    def test_discover_from_local_file(self):
        """Policy loaded from a local file path."""
        from apm_cli.policy.discovery import discover_policy

        fixture = FIXTURES_DIR / "org-policy.yml"
        result = discover_policy(self.project_root, policy_override=str(fixture))

        self.assertTrue(result.found)
        self.assertEqual(result.policy.name, "devexpgbb-test-policy")
        self.assertIn("DevExpGbb/*", result.policy.dependencies.allow)
        self.assertFalse(result.cached)

    def test_discover_minimal_policy(self):
        """Minimal policy with all defaults."""
        from apm_cli.policy.discovery import discover_policy

        fixture = FIXTURES_DIR / "minimal-policy.yml"
        result = discover_policy(self.project_root, policy_override=str(fixture))

        self.assertTrue(result.found)
        self.assertEqual(result.policy.name, "minimal")
        self.assertEqual(result.policy.enforcement, "warn")
        self.assertEqual(result.policy.dependencies.require_resolution, "project-wins")

    def test_discover_enterprise_hub_policy(self):
        """Enterprise hub policy with strict settings."""
        from apm_cli.policy.discovery import discover_policy

        fixture = FIXTURES_DIR / "enterprise-hub-policy.yml"
        result = discover_policy(self.project_root, policy_override=str(fixture))

        self.assertTrue(result.found)
        self.assertEqual(result.policy.enforcement, "block")
        self.assertEqual(result.policy.dependencies.require_resolution, "policy-wins")
        self.assertEqual(result.policy.unmanaged_files.action, "deny")

    def test_discover_invalid_file_returns_error(self):
        """Invalid policy file returns error, not exception."""
        bad_file = self.project_root / "bad-policy.yml"
        bad_file.write_text("enforcement: invalid-value\n", encoding="utf-8")

        from apm_cli.policy.discovery import discover_policy

        result = discover_policy(self.project_root, policy_override=str(bad_file))
        self.assertFalse(result.found)
        self.assertIsNotNone(result.error)

    # -- Cache behaviour --------------------------------------------

    def test_cache_write_and_read(self):
        """Policy is cached after fetch and served from cache on next call."""
        from apm_cli.policy.discovery import _read_cache, _write_cache
        from apm_cli.policy.parser import load_policy as _lp

        policy_yaml = 'name: cached-test\nversion: "1.0.0"\n'
        repo_ref = "test-org/.github"
        policy_obj, _ = _lp(policy_yaml)

        _write_cache(repo_ref, policy_obj, self.project_root)

        result = _read_cache(repo_ref, self.project_root)
        self.assertIsNotNone(result)
        self.assertTrue(result.cached)
        self.assertEqual(result.policy.name, "cached-test")

    def test_cache_respects_ttl(self):
        """Expired cache returns None."""
        from apm_cli.policy.discovery import _read_cache, _write_cache
        from apm_cli.policy.parser import load_policy as _lp

        policy_yaml = 'name: expired-test\nversion: "1.0.0"\n'
        repo_ref = "test-org/.github"
        policy_obj, _ = _lp(policy_yaml)

        _write_cache(repo_ref, policy_obj, self.project_root)

        result = _read_cache(repo_ref, self.project_root, ttl=0)
        self.assertIsNone(result)

    def test_no_cache_bypass(self):
        """File override takes precedence even with populated cache."""
        from apm_cli.policy.discovery import _write_cache, discover_policy
        from apm_cli.policy.parser import load_policy as _lp

        # Pre-populate cache
        policy_yaml = 'name: cached\nversion: "1.0.0"\n'
        policy_obj, _ = _lp(policy_yaml)
        _write_cache("test-org/.github", policy_obj, self.project_root)

        # File override wins regardless of cache state
        fixture = FIXTURES_DIR / "org-policy.yml"
        result = discover_policy(self.project_root, policy_override=str(fixture), no_cache=True)
        self.assertTrue(result.found)
        self.assertEqual(result.policy.name, "devexpgbb-test-policy")

    # -- Policy merging / inheritance -------------------------------

    def test_policy_merge_with_repo_override(self):
        """Repo override merges with org policy via inheritance."""
        from apm_cli.policy.inheritance import merge_policies
        from apm_cli.policy.parser import load_policy

        org_policy, _ = load_policy(FIXTURES_DIR / "org-policy.yml")
        repo_policy, _ = load_policy(FIXTURES_DIR / "repo-override-policy.yml")

        merged = merge_policies(org_policy, repo_policy)

        # Repo adds to deny list (union)
        self.assertIn("experimental/*", merged.dependencies.deny)
        self.assertIn("test-blocked/*", merged.dependencies.deny)
        # Allow list: child omits allow (None = transparent), so parent allow list preserved
        self.assertEqual(merged.dependencies.allow, org_policy.dependencies.allow)

    def test_enterprise_to_org_merge(self):
        """Enterprise hub -> org policy merge chain."""
        from apm_cli.policy.inheritance import resolve_policy_chain
        from apm_cli.policy.parser import load_policy

        enterprise, _ = load_policy(FIXTURES_DIR / "enterprise-hub-policy.yml")
        org, _ = load_policy(FIXTURES_DIR / "org-policy.yml")

        merged = resolve_policy_chain([enterprise, org])

        # Enterprise enforcement=block wins over org enforcement=warn
        self.assertEqual(merged.enforcement, "block")
        # Deny lists are unioned
        self.assertIn("untrusted-vendor/*", merged.dependencies.deny)
        self.assertIn("test-blocked/*", merged.dependencies.deny)
        # Require lists are unioned
        self.assertIn("contoso-governance/coding-standards", merged.dependencies.require)
        self.assertIn("DevExpGbb/required-standards", merged.dependencies.require)
        # Allow list is intersected: common entries between enterprise and org
        self.assertIn("microsoft/*", merged.dependencies.allow)
        self.assertIn("github/*", merged.dependencies.allow)


@unittest.skipUnless(
    POLICY_E2E and HAS_TOKEN,
    "Requires APM_POLICY_E2E_TESTS=1 and GITHUB_APM_PAT",
)
class TestPolicyDiscoveryLiveAPI(unittest.TestCase):
    """Tests that hit real GitHub APIs against DevExpGbb org.

    Repos used:
    - https://github.com/DevExpGbb/.github  (contains apm-policy.yml)
    - https://github.com/DevExpGbb/apm-policy-test-fixture  (has repo override)
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fetch_nonexistent_policy_returns_not_found(self):
        """Fetching from a repo without apm-policy.yml returns not-found."""
        from apm_cli.policy.discovery import _fetch_from_repo

        # microsoft/apm won't have apm-policy.yml
        result = _fetch_from_repo("microsoft/apm", self.project_root)
        self.assertFalse(result.found)

    def test_fetch_devexpgbb_org_policy(self):
        """Fetch real apm-policy.yml from DevExpGbb/.github repo."""
        from apm_cli.policy.discovery import _fetch_from_repo

        result = _fetch_from_repo("DevExpGbb/.github", self.project_root, no_cache=True)
        self.assertTrue(
            result.found, f"Expected policy from DevExpGbb/.github, got error: {result.error}"
        )
        self.assertEqual(result.policy.name, "devexpgbb-test-policy")
        self.assertIn("DevExpGbb/*", result.policy.dependencies.allow)
        self.assertEqual(result.policy.enforcement, "warn")

    def test_fetch_devexpgbb_repo_override(self):
        """Fetch repo-level policy from DevExpGbb/apm-policy-test-fixture."""
        from apm_cli.policy.discovery import _fetch_github_contents

        content, error = _fetch_github_contents(
            "DevExpGbb/apm-policy-test-fixture",
            ".github/apm-policy.yml",
        )
        self.assertIsNone(error, f"Unexpected error: {error}")
        self.assertIsNotNone(content)
        self.assertIn("extends: org", content)
        self.assertIn("experimental/*", content)

    def test_auto_discover_from_cloned_repo(self):
        """Clone test fixture repo and auto-discover org policy."""
        import subprocess

        # Clone the fixture repo into temp dir
        clone_dir = self.project_root / "fixture-clone"
        result = subprocess.run(
            [
                "git",
                "clone",
                "--depth=1",
                "https://github.com/DevExpGbb/apm-policy-test-fixture.git",
                str(clone_dir),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            self.skipTest(f"Clone failed: {result.stderr}")

        from apm_cli.policy.discovery import discover_policy

        fetch_result = discover_policy(clone_dir, no_cache=True)
        self.assertTrue(
            fetch_result.found,
            f"Auto-discovery failed from cloned repo: {fetch_result.error}",
        )
        self.assertEqual(fetch_result.policy.name, "devexpgbb-test-policy")

    def test_fetch_caches_then_serves_from_cache(self):
        """First fetch hits API; second fetch serves from cache."""
        from apm_cli.policy.discovery import _fetch_from_repo

        # First fetch: hits API
        result1 = _fetch_from_repo("DevExpGbb/.github", self.project_root, no_cache=True)
        self.assertTrue(result1.found)
        self.assertFalse(result1.cached)

        # Second fetch: should come from cache
        result2 = _fetch_from_repo("DevExpGbb/.github", self.project_root, no_cache=False)
        self.assertTrue(result2.found)
        self.assertTrue(result2.cached)
        self.assertEqual(result2.policy.name, result1.policy.name)

    def test_merge_org_with_repo_override_live(self):
        """Fetch both org and repo policies from GitHub, merge them."""
        from apm_cli.policy.discovery import _fetch_from_repo
        from apm_cli.policy.inheritance import merge_policies
        from apm_cli.policy.parser import load_policy

        # Fetch org policy
        org_result = _fetch_from_repo("DevExpGbb/.github", self.project_root, no_cache=True)
        self.assertTrue(org_result.found, f"Org policy fetch failed: {org_result.error}")

        # Fetch repo override content
        from apm_cli.policy.discovery import _fetch_github_contents

        content, error = _fetch_github_contents(
            "DevExpGbb/apm-policy-test-fixture",
            ".github/apm-policy.yml",
        )
        self.assertIsNone(error)
        repo_policy, _ = load_policy(content)

        # Merge: repo overrides org (tighten-only)
        merged = merge_policies(org_result.policy, repo_policy)
        self.assertIn("experimental/*", merged.dependencies.deny)
        self.assertIn("test-blocked/*", merged.dependencies.deny)


if __name__ == "__main__":
    unittest.main()
