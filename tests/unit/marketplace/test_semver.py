"""Tests for semver.py -- SemVer parsing, comparison, and range matching."""

from __future__ import annotations

import pytest  # noqa: F401

from apm_cli.marketplace.semver import SemVer, parse_semver, satisfies_range  # noqa: F401

# ---------------------------------------------------------------------------
# parse_semver
# ---------------------------------------------------------------------------


class TestParseSemver:
    """Tests for parse_semver()."""

    def test_plain_version(self) -> None:
        sv = parse_semver("1.2.3")
        assert sv is not None
        assert sv.major == 1
        assert sv.minor == 2
        assert sv.patch == 3
        assert sv.prerelease == ""
        assert sv.build_meta == ""

    def test_prerelease(self) -> None:
        sv = parse_semver("1.0.0-alpha.1")
        assert sv is not None
        assert sv.prerelease == "alpha.1"
        assert sv.is_prerelease

    def test_build_metadata(self) -> None:
        sv = parse_semver("1.0.0+build.42")
        assert sv is not None
        assert sv.build_meta == "build.42"
        assert not sv.is_prerelease

    def test_prerelease_and_build(self) -> None:
        sv = parse_semver("1.0.0-rc.1+build.5")
        assert sv is not None
        assert sv.prerelease == "rc.1"
        assert sv.build_meta == "build.5"
        assert sv.is_prerelease

    def test_zero_version(self) -> None:
        sv = parse_semver("0.0.0")
        assert sv is not None
        assert sv.major == 0
        assert sv.minor == 0
        assert sv.patch == 0

    def test_large_numbers(self) -> None:
        sv = parse_semver("999.888.777")
        assert sv is not None
        assert sv.major == 999
        assert sv.minor == 888
        assert sv.patch == 777

    def test_invalid_not_a_version(self) -> None:
        assert parse_semver("not-a-version") is None

    def test_invalid_two_components(self) -> None:
        assert parse_semver("1.2") is None

    def test_invalid_empty_string(self) -> None:
        assert parse_semver("") is None

    def test_invalid_leading_v(self) -> None:
        # parse_semver requires raw version, no "v" prefix
        assert parse_semver("v1.2.3") is None

    def test_invalid_trailing_text(self) -> None:
        assert parse_semver("1.2.3 extra") is None

    def test_prerelease_with_hyphens(self) -> None:
        sv = parse_semver("1.0.0-alpha-beta")
        assert sv is not None
        assert sv.prerelease == "alpha-beta"


# ---------------------------------------------------------------------------
# SemVer comparison
# ---------------------------------------------------------------------------


class TestSemVerComparison:
    """Tests for SemVer comparison operators."""

    def test_major_ordering(self) -> None:
        assert parse_semver("1.0.0") < parse_semver("2.0.0")  # type: ignore[operator]

    def test_minor_ordering(self) -> None:
        assert parse_semver("1.0.0") < parse_semver("1.1.0")  # type: ignore[operator]

    def test_patch_ordering(self) -> None:
        assert parse_semver("1.0.0") < parse_semver("1.0.1")  # type: ignore[operator]

    def test_prerelease_before_release(self) -> None:
        assert parse_semver("1.0.0-alpha") < parse_semver("1.0.0")  # type: ignore[operator]

    def test_prerelease_alphabetical(self) -> None:
        assert parse_semver("1.0.0-alpha") < parse_semver("1.0.0-beta")  # type: ignore[operator]

    def test_equality(self) -> None:
        assert parse_semver("1.0.0") == parse_semver("1.0.0")

    def test_equality_with_prerelease(self) -> None:
        assert parse_semver("1.0.0-alpha") == parse_semver("1.0.0-alpha")

    def test_not_equal_different_prerelease(self) -> None:
        assert parse_semver("1.0.0-alpha") != parse_semver("1.0.0-beta")

    def test_numeric_prerelease_sorting(self) -> None:
        # Numeric identifiers sort numerically, not lexicographically
        assert parse_semver("1.0.0-2") < parse_semver("1.0.0-10")  # type: ignore[operator]

    def test_numeric_before_alpha_prerelease(self) -> None:
        # Numeric identifiers have lower precedence than alphanumeric
        assert parse_semver("1.0.0-1") < parse_semver("1.0.0-alpha")  # type: ignore[operator]

    def test_build_metadata_ignored_in_equality(self) -> None:
        a = parse_semver("1.0.0+build1")
        b = parse_semver("1.0.0+build2")
        assert a == b

    def test_hashable(self) -> None:
        sv = parse_semver("1.0.0")
        assert sv is not None
        s = {sv}
        assert sv in s

    def test_le(self) -> None:
        assert parse_semver("1.0.0") <= parse_semver("1.0.0")  # type: ignore[operator]
        assert parse_semver("1.0.0") <= parse_semver("2.0.0")  # type: ignore[operator]

    def test_ge(self) -> None:
        assert parse_semver("2.0.0") >= parse_semver("1.0.0")  # type: ignore[operator]
        assert parse_semver("1.0.0") >= parse_semver("1.0.0")  # type: ignore[operator]

    def test_gt(self) -> None:
        assert parse_semver("2.0.0") > parse_semver("1.0.0")  # type: ignore[operator]

    def test_comparison_with_non_semver_returns_not_implemented(self) -> None:
        sv = parse_semver("1.0.0")
        assert sv is not None
        assert sv.__lt__("string") is NotImplemented
        assert sv.__le__("string") is NotImplemented
        assert sv.__gt__("string") is NotImplemented
        assert sv.__ge__("string") is NotImplemented
        assert sv.__eq__("string") is NotImplemented


# ---------------------------------------------------------------------------
# satisfies_range
# ---------------------------------------------------------------------------


class TestSatisfiesRange:
    """Tests for satisfies_range()."""

    # -- Exact match --

    def test_exact_match(self) -> None:
        sv = parse_semver("1.2.3")
        assert sv is not None
        assert satisfies_range(sv, "1.2.3")
        assert not satisfies_range(sv, "1.2.4")

    def test_exact_prerelease(self) -> None:
        sv = parse_semver("1.0.0-alpha")
        assert sv is not None
        assert satisfies_range(sv, "1.0.0-alpha")
        assert not satisfies_range(sv, "1.0.0-beta")

    # -- Caret ranges --

    def test_caret_major_nonzero(self) -> None:
        assert satisfies_range(parse_semver("1.2.3"), "^1.2.3")  # type: ignore[arg-type]
        assert satisfies_range(parse_semver("1.9.9"), "^1.2.3")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("2.0.0"), "^1.2.3")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("1.2.2"), "^1.2.3")  # type: ignore[arg-type]

    def test_caret_zero_major(self) -> None:
        assert satisfies_range(parse_semver("0.2.3"), "^0.2.3")  # type: ignore[arg-type]
        assert satisfies_range(parse_semver("0.2.9"), "^0.2.3")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("0.3.0"), "^0.2.3")  # type: ignore[arg-type]

    def test_caret_zero_zero(self) -> None:
        assert satisfies_range(parse_semver("0.0.3"), "^0.0.3")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("0.0.4"), "^0.0.3")  # type: ignore[arg-type]

    def test_caret_invalid_spec(self) -> None:
        sv = parse_semver("1.0.0")
        assert sv is not None
        assert not satisfies_range(sv, "^garbage")

    # -- Tilde ranges --

    def test_tilde(self) -> None:
        assert satisfies_range(parse_semver("1.2.3"), "~1.2.3")  # type: ignore[arg-type]
        assert satisfies_range(parse_semver("1.2.9"), "~1.2.3")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("1.3.0"), "~1.2.3")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("1.2.2"), "~1.2.3")  # type: ignore[arg-type]

    def test_tilde_invalid_spec(self) -> None:
        sv = parse_semver("1.0.0")
        assert sv is not None
        assert not satisfies_range(sv, "~garbage")

    # -- Comparison operators --

    def test_gte(self) -> None:
        assert satisfies_range(parse_semver("2.0.0"), ">=1.0.0")  # type: ignore[arg-type]
        assert satisfies_range(parse_semver("1.0.0"), ">=1.0.0")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("0.9.0"), ">=1.0.0")  # type: ignore[arg-type]

    def test_gt(self) -> None:
        assert satisfies_range(parse_semver("2.0.0"), ">1.0.0")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("1.0.0"), ">1.0.0")  # type: ignore[arg-type]

    def test_lte(self) -> None:
        assert satisfies_range(parse_semver("1.0.0"), "<=1.0.0")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("1.0.1"), "<=1.0.0")  # type: ignore[arg-type]

    def test_lt(self) -> None:
        assert satisfies_range(parse_semver("0.9.0"), "<1.0.0")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("1.0.0"), "<1.0.0")  # type: ignore[arg-type]

    def test_gt_invalid_spec(self) -> None:
        sv = parse_semver("1.0.0")
        assert sv is not None
        assert not satisfies_range(sv, ">garbage")

    def test_gte_invalid_spec(self) -> None:
        sv = parse_semver("1.0.0")
        assert sv is not None
        assert not satisfies_range(sv, ">=garbage")

    def test_lt_invalid_spec(self) -> None:
        sv = parse_semver("1.0.0")
        assert sv is not None
        assert not satisfies_range(sv, "<garbage")

    def test_lte_invalid_spec(self) -> None:
        sv = parse_semver("1.0.0")
        assert sv is not None
        assert not satisfies_range(sv, "<=garbage")

    # -- Wildcard --

    def test_wildcard_x(self) -> None:
        assert satisfies_range(parse_semver("1.2.0"), "1.2.x")  # type: ignore[arg-type]
        assert satisfies_range(parse_semver("1.2.9"), "1.2.x")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("1.3.0"), "1.2.x")  # type: ignore[arg-type]

    def test_wildcard_star(self) -> None:
        assert satisfies_range(parse_semver("1.2.0"), "1.2.*")  # type: ignore[arg-type]
        assert satisfies_range(parse_semver("1.2.5"), "1.2.*")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("2.2.0"), "1.2.*")  # type: ignore[arg-type]

    def test_wildcard_uppercase_x(self) -> None:
        assert satisfies_range(parse_semver("3.1.7"), "3.1.X")  # type: ignore[arg-type]

    # -- Combined (AND) --

    def test_and_range(self) -> None:
        assert satisfies_range(parse_semver("1.5.0"), ">=1.0.0 <2.0.0")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("2.0.0"), ">=1.0.0 <2.0.0")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("0.9.0"), ">=1.0.0 <2.0.0")  # type: ignore[arg-type]

    # -- Empty range --

    def test_empty_range_matches_all(self) -> None:
        assert satisfies_range(parse_semver("1.0.0"), "")  # type: ignore[arg-type]
        assert satisfies_range(parse_semver("99.0.0"), "  ")  # type: ignore[arg-type]

    # -- Invalid exact spec --

    def test_invalid_exact_spec(self) -> None:
        sv = parse_semver("1.0.0")
        assert sv is not None
        assert not satisfies_range(sv, "garbage")
