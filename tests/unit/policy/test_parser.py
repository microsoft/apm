"""Tests for apm_cli.policy.parser (load_policy, validate_policy)."""

import unittest

import pytest

from apm_cli.policy.parser import (
    _build_policy,
    load_policy,
    validate_policy,
)

pytestmark = pytest.mark.unit


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

    def test_unmanaged_files_must_be_mapping(self):
        for bad in ([], ["x"], "warn", 1):
            errors, warnings = validate_policy({"unmanaged_files": bad})  # noqa: RUF059
            self.assertEqual(len(errors), 1, repr(bad))
            self.assertIn("unmanaged_files must be a YAML mapping", errors[0])

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

    def test_typo_and_wrong_typed_native_values_are_diagnosed(self):
        errors, warnings = validate_policy(
            {
                "version": "1.0",
                "enforcment": True,
                "cache": [],
                "dependencies": {"allow": ""},
            }
        )

        self.assertEqual(warnings, ["Unknown top-level policy key: 'enforcment'"])
        self.assertEqual(
            errors,
            [
                "cache must be a YAML mapping",
                "dependencies.allow must be a YAML list of package patterns",
            ],
        )

    def test_known_policy_blocks_and_pattern_items_use_native_schema_types(self):
        errors, _ = validate_policy(
            {
                "mcp": [],
                "manifest": [],
                "dependencies": {"allow": [123]},
            }
        )

        self.assertEqual(
            errors,
            [
                "mcp must be a YAML mapping",
                "manifest must be a YAML mapping",
                "dependencies.allow must be a YAML list of package patterns",
            ],
        )

    def test_non_dict_input(self):
        errors, warnings = validate_policy("not a dict")  # type: ignore[arg-type]  # noqa: RUF059
        self.assertEqual(len(errors), 1)
        self.assertIn("mapping", errors[0])

    def test_executables_block_valid(self):
        errors, warnings = validate_policy(  # noqa: RUF059
            {
                "executables": {
                    "deny_all": False,
                    "deny": ["bad/pkg"],
                    "require": ["org/hook"],
                    "recommend": ["org/vetted"],
                    "enforce": ["org/mandated"],
                }
            }
        )
        self.assertEqual(errors, [])

    def test_executables_deny_all_must_be_bool(self):
        errors, _ = validate_policy({"executables": {"deny_all": "yes"}})
        self.assertTrue(any("deny_all" in e for e in errors))

    def test_executables_deny_must_be_list(self):
        errors, _ = validate_policy({"executables": {"deny": "bad/pkg"}})
        self.assertTrue(any("executables.deny" in e for e in errors))

    def test_executables_must_be_mapping(self):
        errors, _ = validate_policy({"executables": ["bad/pkg"]})
        self.assertTrue(any("executables must be a YAML mapping" in e for e in errors))

    def test_bin_deploy_emits_deprecation_warning(self):
        _, warnings = validate_policy({"bin_deploy": {"deny_all": True}})
        self.assertTrue(any("deprecated" in w and "bin_deploy" in w for w in warnings))

    def test_enforce_emits_inert_warning(self):
        # M2 (#1873): writing executables.enforce must warn at validate time
        # that it is accepted but INERT in v1 (degrades to recommend).
        errors, warnings = validate_policy({"executables": {"enforce": ["org/mandated"]}})
        self.assertEqual(errors, [])
        self.assertTrue(any("enforce" in w and "INERT" in w and "recommend" in w for w in warnings))

    def test_empty_enforce_does_not_warn(self):
        _, warnings = validate_policy({"executables": {"enforce": []}})
        self.assertFalse(any("enforce" in w and "INERT" in w for w in warnings))

    def test_build_executables_policy(self):
        policy = _build_policy(
            {
                "executables": {
                    "deny_all": True,
                    "deny": ["bad/pkg"],
                    "require": ["org/hook"],
                    "recommend": ["org/vetted"],
                }
            }
        )
        self.assertTrue(policy.executables.deny_all)
        self.assertEqual(policy.executables.deny, ("bad/pkg",))
        self.assertEqual(policy.executables.require, ("org/hook",))
        self.assertEqual(policy.executables.recommend, ("org/vetted",))

    def test_build_executables_default_empty(self):
        policy = _build_policy({"name": "x"})
        self.assertFalse(policy.executables.deny_all)
        self.assertEqual(policy.executables.deny, ())

    def test_multiple_errors(self):
        errors, warnings = validate_policy(  # noqa: RUF059
            {
                "enforcement": "bad",
                "cache": {"ttl": -1},
                "mcp": {"self_defined": "nope"},
            }
        )
        self.assertEqual(len(errors), 3)


class TestSecurityAuditParsing(unittest.TestCase):
    """Validation + build for the security.audit policy section."""

    def test_valid_security_audit(self):
        data = {"security": {"audit": {"on_install": "block", "external": ["skillspector"]}}}
        errors, _ = validate_policy(data)
        self.assertEqual(errors, [])
        policy = _build_policy(data)
        self.assertEqual(policy.security.audit.on_install, "block")
        self.assertEqual(policy.security.audit.external, ("skillspector",))

    def test_invalid_on_install_value(self):
        errors, _ = validate_policy({"security": {"audit": {"on_install": "nope"}}})
        self.assertTrue(errors)
        self.assertIn("on_install", errors[0])

    def test_security_is_known_top_level_key(self):
        # Should not surface an "unknown key" warning.
        _, warnings = validate_policy({"security": {"audit": {"on_install": "warn"}}})
        self.assertFalse(any("security" in w for w in warnings))

    def test_missing_security_defaults_to_none(self):
        policy = _build_policy({})
        self.assertIsNone(policy.security.audit.on_install)
        self.assertIsNone(policy.security.audit.external)


if __name__ == "__main__":
    unittest.main()


class TestMcpTransportAllowCanonicalization(unittest.TestCase):
    """mcp.transport.allow is canonicalized once, at parse time."""

    def test_http_alias_folds_into_streamable_http(self):
        policy, warnings = load_policy(
            "mcp:\n  transport:\n    allow: [HTTP, stdio, streamable-http]\n"
        )
        self.assertEqual(warnings, [])
        self.assertEqual(policy.mcp.transport.allow, ("streamable-http", "stdio"))

    def test_unknown_transport_warns(self):
        policy, warnings = load_policy("mcp:\n  transport:\n    allow: [stdio, websocket]\n")
        self.assertEqual(policy.mcp.transport.allow, ("stdio", "websocket"))
        self.assertEqual(len(warnings), 1)
        self.assertIn("unknown transport 'websocket'", warnings[0])

    def test_inheritance_intersects_canonical_names(self):
        from apm_cli.policy.inheritance import merge_policies

        parent, _ = load_policy("mcp:\n  transport:\n    allow: [stdio, http]\n")
        child, _ = load_policy("mcp:\n  transport:\n    allow: [streamable-http, sse]\n")
        merged = merge_policies(parent, child)
        self.assertEqual(merged.mcp.transport.allow, ("streamable-http",))
