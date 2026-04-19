"""Tests for the install flow with mocked marketplace resolution."""

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.marketplace.resolver import parse_marketplace_ref


class TestInstallMarketplacePreParse:
    """The pre-parse intercept in _validate_and_add_packages_to_apm_yml."""

    def test_marketplace_ref_detected(self):
        """NAME@MARKETPLACE triggers marketplace resolution."""
        result = parse_marketplace_ref("security-checks@acme-tools")
        assert result == ("security-checks", "acme-tools")

    @pytest.mark.parametrize("ref", [
        "owner/repo",
        "owner/repo@alias",
        "just-a-name",
        "git@github.com:o/r",
    ])
    def test_non_marketplace_ref_returns_none(self, ref):
        assert parse_marketplace_ref(ref) is None


class TestValidationOutcomeProvenance:
    """Verify marketplace provenance is attached to ValidationOutcome."""

    def test_outcome_has_provenance_field(self):
        from apm_cli.core.command_logger import _ValidationOutcome

        outcome = _ValidationOutcome(
            valid=[("owner/repo", False)],
            invalid=[],
            marketplace_provenance={
                "owner/repo": {
                    "discovered_via": "acme-tools",
                    "marketplace_plugin_name": "security-checks",
                }
            },
        )
        assert outcome.marketplace_provenance is not None
        assert "owner/repo" in outcome.marketplace_provenance

    def test_outcome_no_provenance(self):
        from apm_cli.core.command_logger import _ValidationOutcome

        outcome = _ValidationOutcome(valid=[], invalid=[])
        assert outcome.marketplace_provenance is None
