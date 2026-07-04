"""Tests for marketplace name sanitisation in output mappers."""

from __future__ import annotations

import pytest

from apm_cli.marketplace.output_mappers import sanitize_marketplace_name


class TestSanitizeMarketplaceName:
    """Verify sanitize_marketplace_name produces valid kebab-case output."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("my.marketplace", "my-marketplace"),
            ("My_Package", "my-package"),
            ("Already-Valid", "already-valid"),
            ("kebab-case-name", "kebab-case-name"),
            ("dots.and_underscores.mixed", "dots-and-underscores-mixed"),
            ("UPPER", "upper"),
            ("with  spaces", "with-spaces"),
            ("multi...dots", "multi-dots"),
            ("--leading-trailing--", "leading-trailing"),
            ("name123", "name123"),
            ("a", "a"),
            ("org/my.marketplace", "org-my-marketplace"),
            ("special!@#chars", "special-chars"),
        ],
    )
    def test_converts_to_kebab_case(self, raw: str, expected: str) -> None:
        assert sanitize_marketplace_name(raw) == expected

    def test_empty_string_returns_fallback(self) -> None:
        assert sanitize_marketplace_name("") == "marketplace"

    def test_only_special_chars_returns_fallback(self) -> None:
        assert sanitize_marketplace_name("...") == "marketplace"

    def test_already_kebab_case_unchanged(self) -> None:
        assert sanitize_marketplace_name("my-marketplace") == "my-marketplace"
