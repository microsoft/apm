"""Unit coverage for the ``_fetch_local`` marketplace fetcher (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.marketplace.client import (
    _validate_ref,
)
from apm_cli.marketplace.errors import MarketplaceFetchError

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-local-authoring-catalog")]


@pytest.mark.parametrize(
    "bad_ref",
    [
        "",
        "-rf",
        "main; rm -rf /",
        "main:other",
        "main with space",
        "..",
    ],
)
def test_validate_ref_rejects_unsafe_inputs(bad_ref: str) -> None:
    with pytest.raises(MarketplaceFetchError, match="Invalid git ref"):
        _validate_ref(bad_ref, "acme")


@pytest.mark.parametrize(
    "good_ref",
    [
        "main",
        "v1.2.3",
        "feature/local-mkt",
        "release-2024.01",
        "abc123",
    ],
)
def test_validate_ref_accepts_safe_inputs(good_ref: str) -> None:
    assert _validate_ref(good_ref, "acme") == good_ref
