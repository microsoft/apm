"""Tests for tag_pattern.py -- render_tag and build_tag_regex."""

from __future__ import annotations

import re

import pytest

from apm_cli.marketplace.tag_pattern import build_tag_regex, render_tag

# ---------------------------------------------------------------------------
# render_tag
# ---------------------------------------------------------------------------


class TestRenderTag:
    """Tests for render_tag placeholder expansion."""

    def test_version_only(self) -> None:
        assert render_tag("v{version}", name="pkg", version="1.2.3") == "v1.2.3"

    def test_name_and_version(self) -> None:
        result = render_tag("{name}-v{version}", name="my-tool", version="0.1.0")
        assert result == "my-tool-v0.1.0"

    def test_bare_version(self) -> None:
        assert render_tag("{version}", name="x", version="2.0.0") == "2.0.0"

    def test_release_prefix(self) -> None:
        result = render_tag("release-{version}", name="x", version="3.0.0")
        assert result == "release-3.0.0"

    def test_name_only_placeholder(self) -> None:
        result = render_tag("{name}-latest", name="tool", version="1.0.0")
        assert result == "tool-latest"

    def test_multiple_version_placeholders(self) -> None:
        result = render_tag("v{version}-{version}", name="x", version="1.0.0")
        assert result == "v1.0.0-1.0.0"

    def test_no_placeholders(self) -> None:
        result = render_tag("fixed-tag", name="x", version="1.0.0")
        assert result == "fixed-tag"

    def test_prerelease_version(self) -> None:
        result = render_tag("v{version}", name="x", version="1.0.0-alpha.1")
        assert result == "v1.0.0-alpha.1"

    def test_version_with_build_metadata(self) -> None:
        result = render_tag("v{version}", name="x", version="1.0.0+build.42")
        assert result == "v1.0.0+build.42"

    def test_empty_name(self) -> None:
        result = render_tag("{name}-v{version}", name="", version="1.0.0")
        assert result == "-v1.0.0"

    def test_special_chars_in_name(self) -> None:
        result = render_tag("{name}-v{version}", name="my.tool", version="1.0.0")
        assert result == "my.tool-v1.0.0"

    def test_complex_pattern(self) -> None:
        result = render_tag(
            "pkg-{name}/release/{version}",
            name="widget",
            version="4.5.6",
        )
        assert result == "pkg-widget/release/4.5.6"


# ---------------------------------------------------------------------------
# build_tag_regex
# ---------------------------------------------------------------------------


class TestBuildTagRegex:
    """Tests for build_tag_regex pattern compilation."""

    def test_v_version_pattern(self) -> None:
        rx = build_tag_regex("v{version}")
        m = rx.match("v1.2.3")
        assert m is not None
        assert m.group("version") == "1.2.3"

    def test_v_version_no_match_garbage(self) -> None:
        rx = build_tag_regex("v{version}")
        assert rx.match("v") is None
        assert rx.match("vabc") is None
        assert rx.match("1.2.3") is None

    def test_bare_version_pattern(self) -> None:
        rx = build_tag_regex("{version}")
        m = rx.match("1.2.3")
        assert m is not None
        assert m.group("version") == "1.2.3"

    def test_name_version_pattern(self) -> None:
        rx = build_tag_regex("{name}-v{version}")
        m = rx.match("my-tool-v1.2.3")
        assert m is not None
        assert m.group("version") == "1.2.3"

    def test_release_prefix_pattern(self) -> None:
        rx = build_tag_regex("release-{version}")
        m = rx.match("release-2.0.0")
        assert m is not None
        assert m.group("version") == "2.0.0"
        assert rx.match("v2.0.0") is None

    def test_prerelease_captured(self) -> None:
        rx = build_tag_regex("v{version}")
        m = rx.match("v1.0.0-alpha.1")
        assert m is not None
        assert m.group("version") == "1.0.0-alpha.1"

    def test_build_metadata_captured(self) -> None:
        rx = build_tag_regex("v{version}")
        m = rx.match("v1.0.0+build.42")
        assert m is not None
        assert m.group("version") == "1.0.0+build.42"

    def test_full_match_only(self) -> None:
        """Pattern anchored at start and end -- no partial match."""
        rx = build_tag_regex("v{version}")
        # prefix text must not match
        assert rx.match("prefix-v1.2.3") is None
        # trailing non-semver text must not match
        assert rx.match("v1.2.3/foo") is None

    def test_prerelease_and_metadata(self) -> None:
        rx = build_tag_regex("v{version}")
        m = rx.match("v1.0.0-rc.1+build.5")
        assert m is not None
        assert m.group("version") == "1.0.0-rc.1+build.5"

    def test_dots_in_pattern_escaped(self) -> None:
        """Dots in the literal portion are escaped (not regex wildcards)."""
        rx = build_tag_regex("pkg.v{version}")
        m = rx.match("pkg.v1.0.0")
        assert m is not None
        # The dot should not match arbitrary chars
        assert rx.match("pkgXv1.0.0") is None

    def test_parens_in_pattern_escaped(self) -> None:
        rx = build_tag_regex("(v{version})")
        m = rx.match("(v1.0.0)")
        assert m is not None
        assert m.group("version") == "1.0.0"

    def test_name_wildcard_non_greedy(self) -> None:
        rx = build_tag_regex("{name}-v{version}")
        m = rx.match("some-pkg-v2.0.0")
        assert m is not None
        assert m.group("version") == "2.0.0"

    def test_complex_pattern(self) -> None:
        rx = build_tag_regex("{name}@{version}")
        m = rx.match("tool@3.1.4")
        assert m is not None
        assert m.group("version") == "3.1.4"

    def test_at_symbol_escaped(self) -> None:
        rx = build_tag_regex("{name}@{version}")
        # '@' is literal in the pattern
        assert rx.match("tool-3.1.4") is None

    def test_multiple_version_not_supported(self) -> None:
        """Second {version} placeholder causes regex error (duplicate group).

        This is expected -- patterns should only contain one {version}.
        """
        with pytest.raises(re.error):
            build_tag_regex("v{version}-{version}")

    def test_no_version_placeholder(self) -> None:
        """Pattern with no {version} still compiles (no capture group)."""
        rx = build_tag_regex("{name}-latest")
        m = rx.match("tool-latest")
        assert m is not None

    def test_caret_in_pattern_escaped(self) -> None:
        rx = build_tag_regex("^v{version}")
        m = rx.match("^v1.0.0")
        assert m is not None

    def test_bracket_in_pattern_escaped(self) -> None:
        rx = build_tag_regex("[v{version}]")
        m = rx.match("[v1.2.3]")
        assert m is not None

    def test_plus_in_pattern_escaped(self) -> None:
        rx = build_tag_regex("v+{version}")
        m = rx.match("v+1.0.0")
        assert m is not None
        assert rx.match("vv1.0.0") is None  # '+' is not a quantifier


# ---------------------------------------------------------------------------
# Round-trip: render_tag -> build_tag_regex -> match
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Verify render_tag output is matched by build_tag_regex."""

    @pytest.mark.parametrize(
        "pattern,name,version",
        [
            ("v{version}", "pkg", "1.2.3"),
            ("{version}", "pkg", "0.0.1"),
            ("{name}-v{version}", "my-tool", "2.0.0"),
            ("release-{version}", "x", "10.20.30"),
            ("{name}@{version}", "tool", "1.0.0-beta.1"),
        ],
    )
    def test_roundtrip(self, pattern: str, name: str, version: str) -> None:
        tag = render_tag(pattern, name=name, version=version)
        rx = build_tag_regex(pattern)
        m = rx.match(tag)
        assert m is not None, f"Pattern {pattern!r} did not match rendered tag {tag!r}"
        if "{version}" in pattern:
            assert m.group("version") == version
