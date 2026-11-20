"""Legacy ``marketplaces.json`` migration test (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.marketplace.models import MarketplaceSource

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-marketplace-raw-structure-diagnostics")]


def test_branch_field_accepted_as_ref_alias() -> None:
    src = MarketplaceSource.from_dict(
        {"name": "alias-mkt", "owner": "a", "repo": "b", "branch": "feature/x"}
    )
    assert src.ref == "feature/x"
