"""Tests for map-form outputs parsing in yml_schema (phase-3a, T-3a-09..26) (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.marketplace.yml_schema import (
    MarketplaceOutputSpec,
)

pytestmark = pytest.mark.unit


class TestOutputSpecFields:
    """T-3a-19..22: MarketplaceOutputSpec dataclass."""

    def test_dataclass_frozen(self) -> None:
        spec = MarketplaceOutputSpec(name="claude", path="x.json")
        with pytest.raises((TypeError, AttributeError)):
            spec.name = "other"  # type: ignore[misc]

    def test_defaults(self) -> None:
        spec = MarketplaceOutputSpec(name="claude", path="x.json")
        assert spec.path_explicit is False

    def test_explicit_path_flag(self) -> None:
        spec = MarketplaceOutputSpec(name="claude", path="x.json", path_explicit=True)
        assert spec.path_explicit is True
