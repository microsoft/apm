"""Tests for the applyTo pattern parser."""

from apm_cli.utils.patterns import parse_apply_to


class TestParseApplyTo:
    """Unit tests for parse_apply_to()."""

    def test_empty_string_returns_empty_list(self):
        assert parse_apply_to("") == []

    def test_whitespace_only_returns_empty_list(self):
        assert parse_apply_to("   ") == []

    def test_single_glob_returns_one_element(self):
        assert parse_apply_to("**/*.py") == ["**/*.py"]

    def test_comma_list_split(self):
        assert parse_apply_to("a,b,c") == ["a", "b", "c"]

    def test_whitespace_trimmed(self):
        assert parse_apply_to("a, b , c") == ["a", "b", "c"]

    def test_trailing_comma_dropped(self):
        assert parse_apply_to("a,b,") == ["a", "b"]

    def test_leading_comma_dropped(self):
        assert parse_apply_to(",a,b") == ["a", "b"]

    def test_single_comma_returns_empty(self):
        assert parse_apply_to(",") == []

    def test_internal_empty_segments_dropped(self):
        assert parse_apply_to("a, ,b") == ["a", "b"]

    def test_realistic_multi_glob(self):
        assert parse_apply_to("**/src/**,**/api/**,**/services/**") == [
            "**/src/**",
            "**/api/**",
            "**/services/**",
        ]

    def test_brace_alternation_not_split(self):
        # Commas inside {...} are glob brace expansion, not list separators.
        assert parse_apply_to("**/*.{css,scss}") == ["**/*.{css,scss}"]

    def test_brace_alternation_mixed_with_top_level_comma(self):
        assert parse_apply_to("**/*.{css,scss},**/*.py") == [
            "**/*.{css,scss}",
            "**/*.py",
        ]

    def test_nested_braces(self):
        assert parse_apply_to("**/{a,{b,c}},**/*.py") == [
            "**/{a,{b,c}}",
            "**/*.py",
        ]
