"""Tests for apm_cli.policy.parser (load_policy, validate_policy)."""

import os
import tempfile
import textwrap
import unittest

from apm_cli.policy.parser import PolicyValidationError, load_policy, validate_policy
from apm_cli.policy.schema import ApmPolicy


class TestValidatePolicy(unittest.TestCase):
    """Test validate_policy on raw dicts."""

    def test_empty_dict_valid(self):
        errors, warnings = validate_policy({})
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_valid_enforcement_values(self):
        for val in ("warn", "block", "off"):
            errors, warnings = validate_policy({"enforcement": val})  # noqa: RUF059
            self.assertEqual(errors, [])

    def test_invalid_enforcement(self):
        errors, warnings = validate_policy({"enforcement": "strict"})  # noqa: RUF059
        self.assertEqual(len(errors), 1)
        self.assertIn("enforcement", errors[0])

    def test_invalid_require_resolution(self):
        errors, warnings = validate_policy({"dependencies": {"require_resolution": "merge"}})  # noqa: RUF059
        self.assertEqual(len(errors), 1)
        self.assertIn("require_resolution", errors[0])

    def test_valid_require_resolution(self):
        for val in ("project-wins", "policy-wins", "block"):
            errors, warnings = validate_policy({"dependencies": {"require_resolution": val}})  # noqa: RUF059
            self.assertEqual(errors, [])

    def test_invalid_self_defined(self):
        errors, warnings = validate_policy({"mcp": {"self_defined": "ignore"}})  # noqa: RUF059
        self.assertEqual(len(errors), 1)
        self.assertIn("self_defined", errors[0])

    def test_valid_self_defined(self):
        for val in ("deny", "warn", "allow"):
            errors, warnings = validate_policy({"mcp": {"self_defined": val}})  # noqa: RUF059
            self.assertEqual(errors, [])

    def test_invalid_scripts(self):
        errors, warnings = validate_policy({"manifest": {"scripts": "warn"}})  # noqa: RUF059
        self.assertEqual(len(errors), 1)
        self.assertIn("scripts", errors[0])

    def test_valid_require_explicit_includes(self):
        for val in (True, False):
            errors, warnings = validate_policy({"manifest": {"require_explicit_includes": val}})
            self.assertEqual(errors, [])
            self.assertEqual(warnings, [])

    def test_invalid_require_explicit_includes_string(self):
        errors, warnings = validate_policy({"manifest": {"require_explicit_includes": "true"}})  # noqa: RUF059
        self.assertEqual(len(errors), 1)
        self.assertIn("require_explicit_includes", errors[0])

    def test_invalid_require_explicit_includes_int(self):
        errors, warnings = validate_policy({"manifest": {"require_explicit_includes": 1}})  # noqa: RUF059
        self.assertEqual(len(errors), 1)
        self.assertIn("require_explicit_includes", errors[0])

    def test_invalid_unmanaged_action(self):
        errors, warnings = validate_policy({"unmanaged_files": {"action": "block"}})  # noqa: RUF059
        self.assertEqual(len(errors), 1)
        self.assertIn("unmanaged_files.action", errors[0])

    def test_negative_cache_ttl(self):
        errors, warnings = validate_policy({"cache": {"ttl": -1}})  # noqa: RUF059
        self.assertEqual(len(errors), 1)
        self.assertIn("cache.ttl", errors[0])

    def test_zero_cache_ttl(self):
        errors, warnings = validate_policy({"cache": {"ttl": 0}})  # noqa: RUF059
        self.assertEqual(len(errors), 1)

    def test_string_cache_ttl(self):
        errors, warnings = validate_policy({"cache": {"ttl": "fast"}})  # noqa: RUF059
        self.assertEqual(len(errors), 1)
        self.assertIn("cache.ttl", errors[0])

    def test_bool_cache_ttl(self):
        errors, warnings = validate_policy({"cache": {"ttl": True}})  # noqa: RUF059
        self.assertEqual(len(errors), 1)

    def test_negative_max_depth(self):
        errors, warnings = validate_policy({"dependencies": {"max_depth": -5}})  # noqa: RUF059
        self.assertEqual(len(errors), 1)
        self.assertIn("max_depth", errors[0])

    def test_string_max_depth(self):
        errors, warnings = validate_policy({"dependencies": {"max_depth": "deep"}})  # noqa: RUF059
        self.assertEqual(len(errors), 1)

    def test_unknown_top_level_keys_no_error(self):
        """Unknown keys produce warnings but are not errors."""
        errors, warnings = validate_policy({"custom_field": True, "name": "test"})
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("custom_field", warnings[0])

    def test_non_dict_input(self):
        errors, warnings = validate_policy("not a dict")  # type: ignore[arg-type]  # noqa: RUF059
        self.assertEqual(len(errors), 1)
        self.assertIn("mapping", errors[0])

    def test_multiple_errors(self):
        errors, warnings = validate_policy(  # noqa: RUF059
            {
                "enforcement": "bad",
                "cache": {"ttl": -1},
                "mcp": {"self_defined": "nope"},
            }
        )
        self.assertEqual(len(errors), 3)


class TestLoadPolicyFromString(unittest.TestCase):
    """Test load_policy with YAML strings."""

    def test_valid_complete_policy(self):
        yaml_str = textwrap.dedent("""\
            name: acme-policy
            version: "1.0"
            enforcement: block
            extends: org
            cache:
              ttl: 1800
            dependencies:
              allow:
                - "contoso/*"
              deny:
                - "evil-corp/**"
              require:
                - "contoso/required-lib"
              require_resolution: policy-wins
              max_depth: 10
            mcp:
              allow:
                - "trusted-mcp/*"
              deny:
                - "bad-server"
              transport:
                allow:
                  - stdio
                  - sse
              self_defined: deny
              trust_transitive: true
            compilation:
              target:
                allow:
                  - vscode
                  - claude
                enforce: vscode
              strategy:
                enforce: distributed
              source_attribution: true
            manifest:
              required_fields:
                - description
                - version
              scripts: deny
              content_types:
                allow:
                  - rules
                  - prompts
            unmanaged_files:
              action: warn
              directories:
                - .github
                - docs
        """)
        policy, warnings = load_policy(yaml_str)

        self.assertEqual(warnings, [])
        self.assertEqual(policy.name, "acme-policy")
        self.assertEqual(policy.version, "1.0")
        self.assertEqual(policy.enforcement, "block")
        self.assertEqual(policy.extends, "org")
        self.assertEqual(policy.cache.ttl, 1800)
        self.assertEqual(policy.dependencies.allow, ("contoso/*",))
        self.assertEqual(policy.dependencies.deny, ("evil-corp/**",))
        self.assertEqual(policy.dependencies.require, ("contoso/required-lib",))
        self.assertEqual(policy.dependencies.require_resolution, "policy-wins")
        self.assertEqual(policy.dependencies.max_depth, 10)
        self.assertEqual(policy.mcp.allow, ("trusted-mcp/*",))
        self.assertEqual(policy.mcp.deny, ("bad-server",))
        self.assertEqual(policy.mcp.transport.allow, ("stdio", "sse"))
        self.assertEqual(policy.mcp.self_defined, "deny")
        self.assertTrue(policy.mcp.trust_transitive)
        self.assertEqual(policy.compilation.target.allow, ("vscode", "claude"))
        self.assertEqual(policy.compilation.target.enforce, "vscode")
        self.assertEqual(policy.compilation.strategy.enforce, "distributed")
        self.assertTrue(policy.compilation.source_attribution)
        self.assertEqual(policy.manifest.required_fields, ("description", "version"))
        self.assertEqual(policy.manifest.scripts, "deny")
        self.assertEqual(policy.manifest.content_types, {"allow": ["rules", "prompts"]})
        self.assertEqual(policy.unmanaged_files.action, "warn")
        self.assertEqual(policy.unmanaged_files.directories, (".github", "docs"))

    def test_minimal_policy(self):
        yaml_str = "name: minimal\nversion: '0.1'"
        policy, warnings = load_policy(yaml_str)
        self.assertEqual(warnings, [])
        self.assertEqual(policy.name, "minimal")
        self.assertEqual(policy.version, "0.1")
        # Everything else should be defaults
        self.assertEqual(policy.enforcement, "warn")
        self.assertEqual(policy.cache.ttl, 3600)
        self.assertIsNone(policy.dependencies.allow)
        self.assertEqual(policy.dependencies.max_depth, 50)
        self.assertFalse(policy.manifest.require_explicit_includes)

    def test_require_explicit_includes_true(self):
        yaml_str = textwrap.dedent("""
            manifest:
              require_explicit_includes: true
        """)
        policy, warnings = load_policy(yaml_str)
        self.assertEqual(warnings, [])
        self.assertTrue(policy.manifest.require_explicit_includes)

    def test_require_explicit_includes_no_unknown_warning(self):
        yaml_str = textwrap.dedent("""
            manifest:
              require_explicit_includes: false
        """)
        policy, warnings = load_policy(yaml_str)
        self.assertEqual(warnings, [])
        self.assertFalse(policy.manifest.require_explicit_includes)

    def test_empty_yaml(self):
        policy, warnings = load_policy("")  # noqa: RUF059
        self.assertIsInstance(policy, ApmPolicy)
        self.assertEqual(policy.name, "")

    def test_invalid_enforcement_raises(self):
        with self.assertRaises(PolicyValidationError) as ctx:
            load_policy("enforcement: strict")
        self.assertIn("enforcement", ctx.exception.errors[0])

    def test_invalid_require_resolution_raises(self):
        yaml_str = "dependencies:\n  require_resolution: merge"
        with self.assertRaises(PolicyValidationError):
            load_policy(yaml_str)

    def test_invalid_self_defined_raises(self):
        yaml_str = "mcp:\n  self_defined: ignore"
        with self.assertRaises(PolicyValidationError):
            load_policy(yaml_str)

    def test_invalid_cache_ttl_negative(self):
        with self.assertRaises(PolicyValidationError):
            load_policy("cache:\n  ttl: -10")

    def test_invalid_cache_ttl_string(self):
        with self.assertRaises(PolicyValidationError):
            load_policy("cache:\n  ttl: fast")

    def test_nested_missing_sections_use_defaults(self):
        yaml_str = textwrap.dedent("""\
            name: partial
            dependencies:
              allow:
                - "org/*"
        """)
        policy, warnings = load_policy(yaml_str)  # noqa: RUF059
        self.assertEqual(policy.dependencies.allow, ("org/*",))
        self.assertEqual(policy.dependencies.deny, ())
        self.assertEqual(policy.dependencies.max_depth, 50)
        self.assertEqual(policy.mcp.self_defined, "warn")

    def test_extends_org(self):
        policy, warnings = load_policy("extends: org")  # noqa: RUF059
        self.assertEqual(policy.extends, "org")

    def test_extends_owner_repo(self):
        policy, warnings = load_policy("extends: acme/policies")  # noqa: RUF059
        self.assertEqual(policy.extends, "acme/policies")

    def test_extends_url(self):
        policy, warnings = load_policy("extends: https://example.com/policy.yml")  # noqa: RUF059
        self.assertEqual(policy.extends, "https://example.com/policy.yml")

    def test_malformed_yaml_raises(self):
        with self.assertRaises(PolicyValidationError) as ctx:
            load_policy(":\n  bad:\n- yaml: [")
        self.assertTrue(any("YAML parse error" in e for e in ctx.exception.errors))

    def test_yaml_list_not_mapping_raises(self):
        with self.assertRaises(PolicyValidationError):
            load_policy("- item1\n- item2")

    def test_version_coerced_to_string(self):
        policy, warnings = load_policy("version: '2.0'")  # noqa: RUF059
        self.assertEqual(policy.version, "2.0")

    def test_long_yaml_string_does_not_crash(self):
        """Long YAML strings (> PATH_MAX on macOS) must not raise OSError."""
        # Build a YAML payload larger than typical PATH_MAX limits (1024 bytes)
        # so that Path.is_file() can raise ENAMETOOLONG on macOS.
        long_comment = "# " + "x" * 2048 + "\n"
        yaml_str = long_comment + "name: long-policy\n" + "version: '1.0'\n" + "enforcement: off\n"
        # Ensure the string is long enough to trigger ENAMETOOLONG on macOS
        self.assertGreater(len(yaml_str), 1024)

        # This should parse as inline YAML, not as a file path
        policy, warnings = load_policy(yaml_str)  # noqa: RUF059
        self.assertEqual(policy.name, "long-policy")
        self.assertEqual(policy.version, "1.0")
        self.assertEqual(policy.enforcement, "off")


class TestLoadPolicyFromFile(unittest.TestCase):
    """Test load_policy from a file path."""

    def test_load_from_file(self):
        yaml_content = textwrap.dedent("""\
            name: file-policy
            version: "1.0"
            enforcement: off
        """)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            path = f.name

        try:
            policy, warnings = load_policy(path)  # noqa: RUF059
            self.assertEqual(policy.name, "file-policy")
            self.assertEqual(policy.enforcement, "off")
        finally:
            os.unlink(path)

    def test_load_from_pathlib_path(self):
        from pathlib import Path

        yaml_content = "name: pathlib-test\nversion: '0.1'"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            path = Path(f.name)

        try:
            policy, warnings = load_policy(path)  # noqa: RUF059
            self.assertEqual(policy.name, "pathlib-test")
        finally:
            os.unlink(str(path))


if __name__ == "__main__":
    unittest.main()
