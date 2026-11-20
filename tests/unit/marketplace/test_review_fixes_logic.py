"""Regression tests for PR #1038 review-comment fixes (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.marketplace.yml_editor import _is_apm_yml_with_marketplace

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# r3: malformed apm.yml surfaces a clear error
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# r4: migrate handles empty / non-mapping apm.yml
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# r5: _is_apm_yml_with_marketplace rejects non-mapping marketplace values
# ---------------------------------------------------------------------------


class TestIsApmYmlWithMarketplaceTightened:
    def test_marketplace_value_must_be_mapping(self):
        # Scalar marketplace value is not a valid block; helper must say no.
        data = {"name": "app", "marketplace": "not-a-block"}
        assert _is_apm_yml_with_marketplace(data) is False

    def test_marketplace_list_value_rejected(self):
        data = {"name": "app", "marketplace": [1, 2, 3]}
        assert _is_apm_yml_with_marketplace(data) is False

    def test_marketplace_mapping_accepted(self):
        data = {"name": "app", "marketplace": {"owner": {"name": "acme"}}}
        assert _is_apm_yml_with_marketplace(data) is True

    def test_missing_or_null_block_rejected(self):
        assert _is_apm_yml_with_marketplace({"name": "app"}) is False
        assert _is_apm_yml_with_marketplace({"marketplace": None}) is False


# ---------------------------------------------------------------------------
# r6: marketplace init handles empty / non-mapping apm.yml
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Followup: migrate -- malformed apm.yml raises typed MarketplaceYmlError
# (instead of leaking a raw ruamel.yaml.YAMLError)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Followup: apm init --marketplace warns when experimental flag is disabled
# ---------------------------------------------------------------------------

# Removed: the ``marketplace_authoring`` experimental flag was deleted when
# marketplace authoring went GA. ``apm init --marketplace`` now appends the
# block unconditionally, so the disabled-flag warning case no longer exists.
