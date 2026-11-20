"""Unit coverage for the URL-first marketplace source parser (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise(
        "P-OWN-marketplace-raw-structure-diagnostics", "P-OWN-marketplace-source-admission"
    ),
]


def test_marketplace_origin_keeps_hosted_url_and_skips_local(tmp_path) -> None:
    from apm_cli.marketplace.models import MarketplaceSource
    from apm_cli.marketplace.source_identity import marketplace_origin

    hosted = "https://catalog.example.com/v1/marketplace.json"
    assert marketplace_origin(MarketplaceSource(name="hosted", url=hosted, path="")) == hosted
    local = MarketplaceSource(name="local", url=str(tmp_path))
    assert marketplace_origin(local) == ""
