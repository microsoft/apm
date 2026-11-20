"""Lockstep tests pinning the documented forms of ``--policy`` / ``--policy-source`` (pure logic cases)."""

import pytest

from apm_cli.policy._help_text import POLICY_SOURCE_FORMS_HELP

pytestmark = pytest.mark.unit

EXPECTED_FORM_TOKENS = ("'org'", "owner/repo", "https://", "file path")


def test_canonical_constant_lists_all_supported_forms():
    """The constant text mentions every form ``discover_policy`` accepts."""
    for token in EXPECTED_FORM_TOKENS:
        assert token in POLICY_SOURCE_FORMS_HELP, (
            f"POLICY_SOURCE_FORMS_HELP missing canonical form: {token!r}. "
            "If discover_policy stopped accepting this form, the change "
            "is intentional and this test should be updated. Otherwise "
            "the constant has drifted from the parser."
        )
