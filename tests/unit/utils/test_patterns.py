"""Tests for the applyTo pattern parser."""

from apm_cli.utils.patterns import has_top_level_comma, parse_apply_to, yaml_double_quote


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


class TestHasTopLevelComma:
    """Unit tests for has_top_level_comma()."""

    def test_no_comma(self):
        assert has_top_level_comma("**/*.py") is False

    def test_top_level_comma(self):
        assert has_top_level_comma("a,b") is True

    def test_brace_comma_only(self):
        # Commas inside {...} are brace expansion, not separators.
        assert has_top_level_comma("**/*.{css,scss}") is False

    def test_brace_comma_and_top_level(self):
        assert has_top_level_comma("**/*.{css,scss},**/*.py") is True

    def test_nested_braces(self):
        assert has_top_level_comma("**/{a,{b,c}}") is False

    def test_empty(self):
        assert has_top_level_comma("") is False


class TestYamlDoubleQuote:
    """Unit tests for yaml_double_quote() defence-in-depth escaping."""

    def test_plain_glob(self):
        assert yaml_double_quote("**/*.py") == '"**/*.py"'

    def test_escapes_double_quote(self):
        assert yaml_double_quote('a"b') == '"a\\"b"'

    def test_escapes_backslash(self):
        assert yaml_double_quote("a\\b") == '"a\\\\b"'

    def test_escapes_newline(self):
        assert yaml_double_quote("a\nb") == '"a\\nb"'

    def test_escapes_carriage_return(self):
        assert yaml_double_quote("a\rb") == '"a\\rb"'

    def test_escapes_tab(self):
        assert yaml_double_quote("a\tb") == '"a\\tb"'

    def test_yaml_safe_load_roundtrip(self):
        import yaml

        for value in ['a"b', "a\\b", "a\nb", "**/src/**", "**/*.{css,scss}"]:
            yaml_doc = f"k: {yaml_double_quote(value)}\n"
            assert yaml.safe_load(yaml_doc) == {"k": value}
