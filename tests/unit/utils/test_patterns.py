"""Tests for the applyTo pattern parser."""

import yaml

from apm_cli.utils.patterns import (
    escape_apply_to_segment,
    has_top_level_comma,
    literal_apply_to_top_level_roots,
    normalize_apply_to,
    parse_apply_to,
    yaml_double_quote,
    yaml_globs_scalar,
    yaml_plain_scalar,
)


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

    def test_character_class_comma_is_not_a_list_separator(self):
        assert parse_apply_to("src/[a,b]/**/*.py,docs/**/*.md") == [
            "src/[a,b]/**/*.py",
            "docs/**/*.md",
        ]

    def test_nested_braces(self):
        assert parse_apply_to("**/{a,{b,c}},**/*.py") == [
            "**/{a,{b,c}}",
            "**/*.py",
        ]

    def test_escaped_comma_is_not_split(self):
        assert parse_apply_to(r"src/foo\,bar/*.py,**/*.pyi") == [
            "src/foo,bar/*.py",
            "**/*.pyi",
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

    def test_escaped_comma_is_not_top_level(self):
        assert has_top_level_comma(r"src/foo\,bar/*.py") is False


class TestNormalizeApplyTo:
    """Unit tests for YAML-list applyTo normalization."""

    def test_list_trims_entries_and_discards_empty_values(self):
        normalized = normalize_apply_to(["  **/*.py  ", None, "", "  ", "**/*.md"])

        assert normalized == "**/*.py,**/*.md"

    def test_list_literal_comma_preserves_pattern_boundary(self):
        normalized = normalize_apply_to(["src/foo,bar/*.py", "**/*.pyi"])

        assert parse_apply_to(normalized) == ["src/foo,bar/*.py", "**/*.pyi"]


class TestEscapeApplyToSegment:
    """Unit tests for escape_apply_to_segment() (issue #3011 Copilot follow-up).

    A target that must comma-join multiple already-parsed globs back into
    one scalar (Cursor's ``globs:``) needs to re-escape any literal
    top-level comma before joining, or that comma becomes indistinguishable
    from the separator it just added.
    """

    def test_plain_pattern_is_unchanged(self):
        assert escape_apply_to_segment("src/**/*.py") == "src/**/*.py"

    def test_literal_comma_is_escaped(self):
        assert escape_apply_to_segment("src/foo,bar/*.py") == "src/foo\\,bar/*.py"

    def test_literal_backslash_is_doubled(self):
        assert escape_apply_to_segment("src\\foo/*.py") == "src\\\\foo/*.py"

    def test_brace_alternation_comma_is_not_escaped(self):
        """Commas inside {...} are glob syntax, not applyTo separators."""
        assert escape_apply_to_segment("**/*.{css,scss}") == "**/*.{css,scss}"

    def test_round_trips_through_parse_apply_to(self):
        joined = ", ".join(escape_apply_to_segment(g) for g in ["src/foo,bar/*.py", "**/*.pyi"])
        assert parse_apply_to(joined) == ["src/foo,bar/*.py", "**/*.pyi"]


class TestLiteralApplyToTopLevelRoots:
    """Tests for conservative traversal-root analysis."""

    def test_unions_literal_roots_across_expressions_and_comma_lists(self):
        roots = literal_apply_to_top_level_roots(
            ["src/**/*.py,docs/**/*.md", "packages/*/src/**/*.py"]
        )

        assert roots == frozenset({"src", "docs", "packages"})

    def test_retains_literal_prefix_before_later_brace_glob(self):
        assert literal_apply_to_top_level_roots(["src/{api,cli}/**"]) == frozenset({"src"})

    def test_returns_none_for_global_or_unprovable_patterns(self):
        unprovable = [
            [None],
            [""],
            ["**/*.py"],
            ["*.py"],
            ["*/src/**/*.py"],
            ["[sd]rc/**/*.py"],
            ["{src,docs}/**"],
            ["src/**/*.py,**/*.md"],
            ["src/{api,cli/**"],
            ["/src/**/*.py"],
            ["../src/**/*.py"],
            [r"src\**\*.py"],
            [r"src/foo\,bar/**/*.py"],
        ]

        for patterns in unprovable:
            assert literal_apply_to_top_level_roots(patterns) is None


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

        values = ['a"b', "a\\b", "a\nb", "**/src/**", "**/*.{css,scss}"]
        values.extend(chr(codepoint) for codepoint in range(32))
        values.append(chr(127))
        for value in values:
            yaml_doc = f"k: {yaml_double_quote(value)}\n"
            assert yaml_doc.isascii()
            assert yaml.safe_load(yaml_doc) == {"k": value}


class TestYamlPlainScalar:
    """Unit tests for yaml_plain_scalar() (issue #3002).

    Cursor's own ``.mdc`` frontmatter docs never show quoted ``globs``/
    ``description`` values -- every example is a bare plain scalar. This
    helper prefers that form and only quotes when YAML correctness
    requires it, without ever forcing ``\\uXXXX`` escaping of printable
    non-ASCII text.
    """

    def test_plain_glob_stays_bare(self):
        assert yaml_plain_scalar("src/components/**/*.tsx") == "src/components/**/*.tsx"

    def test_comma_joined_globs_stay_bare(self):
        value = "docs/**/*.md, docs/**/*.mdx"
        assert yaml_plain_scalar(value) == value

    def test_plain_description_stays_bare(self):
        assert yaml_plain_scalar("Python service patterns") == "Python service patterns"

    def test_leading_double_star_glob_gets_single_quoted(self):
        assert yaml_plain_scalar("**/*.py") == "'**/*.py'"

    def test_leading_double_star_comma_list_gets_single_quoted(self):
        value = "**/src/**, **/api/**, **/services/**"
        assert yaml_plain_scalar(value) == f"'{value}'"

    def test_mid_word_apostrophe_stays_bare(self):
        """An apostrophe is only a YAML indicator at position 0; mid-word it's plain-safe."""
        assert yaml_plain_scalar("it's a test") == "it's a test"

    def test_literal_single_quote_is_doubled_when_quoting_required(self):
        assert yaml_plain_scalar("*it's a test") == "'*it''s a test'"

    def test_non_ascii_stays_literal_utf8_when_plain(self):
        value = "Python service patterns \u2014 async \u2192 sync"
        result = yaml_plain_scalar(value)
        assert result == value
        assert "\\u" not in result

    def test_non_ascii_stays_literal_utf8_when_quoted(self):
        value = "* \u2014 async \u2192 sync"
        result = yaml_plain_scalar(value)
        assert result == f"'{value}'"
        assert "\\u" not in result

    def test_colon_space_gets_quoted(self):
        assert yaml_plain_scalar("Foo: Bar") == "'Foo: Bar'"

    def test_reserved_word_gets_quoted(self):
        assert yaml_plain_scalar("true") == "'true'"
        assert yaml_plain_scalar("null") == "'null'"

    def test_embedded_newline_falls_back_to_double_quote(self):
        result = yaml_plain_scalar("safe\n---")
        assert result == '"safe\\n---"'

    def test_embedded_newline_with_non_ascii_preserves_utf8(self):
        value = "safe\u2014line\nbreak"
        result = yaml_plain_scalar(value)
        assert "\\u" not in result
        assert "\u2014" in result

    def test_never_forces_ascii_escaping(self):
        """Unlike yaml_double_quote, this never emits \\uXXXX for printable text."""
        values = [
            "Python service patterns \u2014 async \u2192 sync",
            "* \u2014 leading star forces quoting",
            "embedded\nnewline \u2014 forces double-quote fallback",
        ]
        for value in values:
            assert "\\u" not in yaml_plain_scalar(value)

    def test_yaml_safe_load_roundtrip(self):
        import yaml

        values = [
            "**/*.py",
            "docs/**/*.md, docs/**/*.mdx",
            "it's a test",
            "Foo: Bar",
            "true",
            "safe\n---",
            "Python service patterns \u2014 async \u2192 sync",
            "",
            "   ",
        ]
        values.extend(chr(codepoint) for codepoint in range(32))
        for value in values:
            yaml_doc = f"k: {yaml_plain_scalar(value)}\n"
            assert yaml.safe_load(yaml_doc) == {"k": value}

    def test_del_control_char_escapes_to_uXXXX(self):
        """U+007F (DEL) is outside YAML 1.1's printable set; must be \\u-escaped."""
        value = "safe\x7ftext"
        result = yaml_plain_scalar(value)
        assert result == '"safe\\u007ftext"'
        assert yaml.safe_load(f"k: {result}\n") == {"k": value}

    def test_line_separator_forces_double_quote_and_escapes(self):
        """U+2028 (LS) is a YAML line-break char; a naive round-trip check can
        be fooled by PyYAML's own line-folding into reporting a false match,
        so this must go straight to the double-quoted, \\u-escaped fallback.
        """
        value = "safe\u2028text"
        result = yaml_plain_scalar(value)
        assert result == '"safe\\u2028text"'
        assert yaml.safe_load(f"k: {result}\n") == {"k": value}

    def test_paragraph_separator_adjacent_to_newline_round_trips(self):
        """Regression: U+2029 immediately followed by \\n previously
        spuriously round-tripped as bare due to PyYAML folding two adjacent
        line-break-like characters back into the original text.
        """
        value = "safe\u2029\ntext"
        result = yaml_plain_scalar(value)
        assert "\u2029" not in result
        assert yaml.safe_load(f"k: {result}\n") == {"k": value}

    def test_next_line_control_char_escapes_to_uXXXX(self):
        value = "safe\u0085text"
        result = yaml_plain_scalar(value)
        assert result == '"safe\\u0085text"'
        assert yaml.safe_load(f"k: {result}\n") == {"k": value}

    def test_round_trip_check_stays_bounded_for_flow_style_alias_bomb(self):
        """A ``description`` is untrusted content; the round-trip check must
        route through the project's bounded YAML loader (issue #3011
        Copilot follow-up), not stock ``yaml.safe_load``, or a description
        whose literal text is itself a flow-style merge-key/alias expansion
        bomb could exhaust memory/CPU on this second, in-memory parse.
        Written entirely on one line (flow style) so it isn't trivially
        skipped by the newline-triggers-double-quote-fallback branch.
        """
        parts = ["a0: &a0 {k: v}"]
        prev = "a0"
        for i in range(1, 40):
            cur = f"a{i}"
            parts.append(f"{cur}: &{cur} {{<<: [*{prev}, *{prev}]}}")
            prev = cur
        bomb = "{" + ", ".join(parts) + "}"

        result = yaml_plain_scalar(bomb)

        assert yaml.safe_load(f"k: {result}\n") == {"k": bomb}

    def test_lone_surrogate_escapes_instead_of_crashing_utf8_encode(self):
        """PR #3011 Copilot follow-up regression: a lone (unpaired) UTF-16
        surrogate -- the project's existing hidden-unicode security-gate
        attack shape, e.g. a description escaped as ``\\uDB40\\uDC01`` --
        cannot be UTF-8 encoded at all. It must never reach the bare or
        single-quoted path: it has to fall back to a \\uXXXX-escaped
        double-quoted scalar so writing the rendered file to disk doesn't
        raise ``UnicodeEncodeError``.
        """
        value = "\ud83c\udff4hidden"  # decodes from \uD83C\uDFF4 -- a real
        # supplementary-plane character in UTF-16 terms, but Python stores
        # it as two lone surrogate code points when built from separate
        # \u escapes like this, matching how a hostile description arrives
        # after YAML frontmatter decoding.
        result = yaml_plain_scalar(value)
        assert result == '"\\ud83c\\udff4hidden"'
        result.encode("utf-8")  # must not raise UnicodeEncodeError
        assert yaml.safe_load(f"k: {result}\n") == {"k": value}


class TestYamlGlobsScalar:
    """Unit tests for yaml_globs_scalar() (issue #3002 follow-up).

    Cursor's ``globs`` docs never show a quoted value, not even for
    patterns starting with ``**`` -- unlike free-form ``description``
    text, glob syntax has no legitimate use for the YAML ambiguities
    (reserved words, ``: ``, a leading alias-like ``*``) that
    :func:`yaml_plain_scalar` guards against, so ``globs`` always stays
    bare except for the one thing that would actually corrupt the
    frontmatter block: an embedded newline.
    """

    def test_plain_glob_stays_bare(self):
        assert yaml_globs_scalar("src/components/**/*.tsx") == "src/components/**/*.tsx"

    def test_leading_double_star_glob_stays_bare(self):
        assert yaml_globs_scalar("**/*.py") == "**/*.py"

    def test_leading_double_star_comma_list_stays_bare(self):
        value = "**/src/**, **/api/**, **/services/**"
        assert yaml_globs_scalar(value) == value

    def test_reserved_word_stays_bare(self):
        """Unlike yaml_plain_scalar, globs never quote for YAML type ambiguity."""
        assert yaml_globs_scalar("true") == "true"

    def test_embedded_newline_falls_back_to_double_quote(self):
        result = yaml_globs_scalar("safe\n---")
        assert result == '"safe\\n---"'

    def test_embedded_carriage_return_falls_back_to_double_quote(self):
        result = yaml_globs_scalar("safe\r---")
        assert result == '"safe\\r---"'

    def test_never_forces_ascii_escaping(self):
        value = "docs/\u2014/**"
        result = yaml_globs_scalar(value)
        assert "\\u" not in result
        assert value == result

    def test_del_control_char_falls_back_to_double_quote(self):
        value = "safe\x7ftext"
        result = yaml_globs_scalar(value)
        assert result == '"safe\\u007ftext"'
        assert yaml.safe_load(f"k: {result}\n") == {"k": value}

    def test_line_separator_falls_back_to_double_quote(self):
        value = "safe\u2028text"
        result = yaml_globs_scalar(value)
        assert result == '"safe\\u2028text"'
        assert yaml.safe_load(f"k: {result}\n") == {"k": value}

    def test_lone_surrogate_escapes_instead_of_crashing_utf8_encode(self):
        value = "\ud83c\udff4hidden"
        result = yaml_globs_scalar(value)
        assert result == '"\\ud83c\\udff4hidden"'
        result.encode("utf-8")
        assert yaml.safe_load(f"k: {result}\n") == {"k": value}
