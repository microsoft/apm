"""TDD seams for issue #1778 -- declarable integrity policy keys.

Covers the two additive, default-off keys nested under the existing
``security:`` namespace:

* ``security.integrity.require_hashes`` -- require lockfile content hashes
  for all installs; a MISSING hash is a failure (fail-closed).
* ``security.audit.fail_on_drift`` -- make ``apm audit`` exit non-zero when
  workspace content drifts from the lockfile.

Three mandatory seams live here (None-transparency on merge + fail-closed for
``require_hashes``); the golden parse-equivalence seam is in
``test_policy_golden.py`` and the ``fail_on_drift`` exit-code seam is in
``tests/unit/test_audit_fail_on_drift.py``.
"""

from __future__ import annotations

import unittest

from apm_cli.policy.inheritance import merge_policies
from apm_cli.policy.parser import load_policy, validate_policy
from apm_cli.policy.schema import (
    ApmPolicy,
    AuditPolicy,
    IntegrityPolicy,
    SecurityPolicy,
)


class TestIntegritySchemaDefaults(unittest.TestCase):
    """Both keys default OFF so existing policies are unchanged."""

    def test_integrity_default_off(self):
        self.assertFalse(IntegrityPolicy().require_hashes)

    def test_security_has_integrity_block(self):
        sec = SecurityPolicy()
        self.assertIsInstance(sec.integrity, IntegrityPolicy)
        self.assertFalse(sec.integrity.require_hashes)

    def test_audit_fail_on_drift_default_off(self):
        self.assertFalse(AuditPolicy().fail_on_drift)

    def test_apm_policy_defaults_off(self):
        p = ApmPolicy()
        self.assertFalse(p.security.integrity.require_hashes)
        self.assertFalse(p.security.audit.fail_on_drift)

    def test_frozen(self):
        ig = IntegrityPolicy()
        with self.assertRaises(AttributeError):
            ig.require_hashes = True  # type: ignore[misc]


class TestIntegrityParsing(unittest.TestCase):
    """The parser reads both keys from the security namespace."""

    def test_require_hashes_parsed(self):
        policy, _ = load_policy("name: p\nsecurity:\n  integrity:\n    require_hashes: true\n")
        self.assertTrue(policy.security.integrity.require_hashes)

    def test_fail_on_drift_parsed(self):
        policy, _ = load_policy("name: p\nsecurity:\n  audit:\n    fail_on_drift: true\n")
        self.assertTrue(policy.security.audit.fail_on_drift)

    def test_both_default_off_when_silent(self):
        policy, _ = load_policy("name: p\nsecurity:\n  audit:\n    on_install: warn\n")
        self.assertFalse(policy.security.integrity.require_hashes)
        self.assertFalse(policy.security.audit.fail_on_drift)

    def test_no_unknown_key_warning(self):
        _, warnings = load_policy(
            "name: p\nsecurity:\n  integrity:\n    require_hashes: true\n"
            "  audit:\n    fail_on_drift: true\n"
        )
        joined = " ".join(warnings)
        self.assertNotIn("integrity", joined)
        self.assertNotIn("fail_on_drift", joined)


class TestIntegrityValidation(unittest.TestCase):
    """Non-boolean values are rejected (no false-assurance via typos)."""

    def test_require_hashes_must_be_bool(self):
        errors, _ = validate_policy({"security": {"integrity": {"require_hashes": "yes"}}})
        self.assertTrue(any("require_hashes" in e for e in errors))

    def test_fail_on_drift_must_be_bool(self):
        errors, _ = validate_policy({"security": {"audit": {"fail_on_drift": "soon"}}})
        self.assertTrue(any("fail_on_drift" in e for e in errors))

    def test_integrity_must_be_mapping(self):
        errors, _ = validate_policy({"security": {"integrity": ["nope"]}})
        self.assertTrue(any("integrity" in e for e in errors))

    def test_valid_booleans_pass(self):
        errors, _ = validate_policy(
            {
                "security": {
                    "integrity": {"require_hashes": True},
                    "audit": {"fail_on_drift": False},
                }
            }
        )
        self.assertEqual(errors, [])


class TestNoneTransparencyOnMerge(unittest.TestCase):
    """Seam 2: a child silent on a new key does NOT relax a parent that set it."""

    def test_require_hashes_parent_set_child_silent(self):
        parent = ApmPolicy(security=SecurityPolicy(integrity=IntegrityPolicy(require_hashes=True)))
        merged = merge_policies(parent, ApmPolicy())
        self.assertTrue(merged.security.integrity.require_hashes)

    def test_fail_on_drift_parent_set_child_silent(self):
        parent = ApmPolicy(security=SecurityPolicy(audit=AuditPolicy(fail_on_drift=True)))
        merged = merge_policies(parent, ApmPolicy())
        self.assertTrue(merged.security.audit.fail_on_drift)

    def test_child_can_tighten(self):
        child = ApmPolicy(
            security=SecurityPolicy(
                integrity=IntegrityPolicy(require_hashes=True),
                audit=AuditPolicy(fail_on_drift=True),
            )
        )
        merged = merge_policies(ApmPolicy(), child)
        self.assertTrue(merged.security.integrity.require_hashes)
        self.assertTrue(merged.security.audit.fail_on_drift)

    def test_child_cannot_relax_parent(self):
        parent = ApmPolicy(
            security=SecurityPolicy(
                integrity=IntegrityPolicy(require_hashes=True),
                audit=AuditPolicy(fail_on_drift=True),
            )
        )
        # Child explicitly off must not relax the parent's tightened floor.
        child = ApmPolicy(
            security=SecurityPolicy(
                integrity=IntegrityPolicy(require_hashes=False),
                audit=AuditPolicy(fail_on_drift=False),
            )
        )
        merged = merge_policies(parent, child)
        self.assertTrue(merged.security.integrity.require_hashes)
        self.assertTrue(merged.security.audit.fail_on_drift)

    def test_on_install_still_carried(self):
        # Regression: adding the new keys must not drop the existing on_install.
        parent = ApmPolicy(security=SecurityPolicy(audit=AuditPolicy(on_install="block")))
        merged = merge_policies(parent, ApmPolicy())
        self.assertEqual(merged.security.audit.on_install, "block")


if __name__ == "__main__":
    unittest.main()
