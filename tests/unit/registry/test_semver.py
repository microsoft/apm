"""Tests for registry wrappers around the canonical APM semver matcher.

Covers the parse-time gate (``is_semver_range``) that rejects branch names and
commit SHAs when an entry routes to a registry, plus the range-matching logic
used by the resolver to pick the best published version.
"""

from __future__ import annotations

import pytest

from apm_cli.deps.registry.semver import (
    is_semver_range,
    match_version,
    pick_best,
)


class TestIsSemverRange:
    """``is_semver_range`` is the parse-time gate for registry-routed deps."""

    @pytest.mark.parametrize(
        "spec",
        [
            "1.0.0",
            "^1.0.0",
            "^1.2.3",
            "~1.2.3",
            ">=1.0.0",
            "<2.0.0",
            ">=1.0.0 <2.0.0",
            ">1.0.0",
            "<=2.0.0",
            "1.2.x",
            "1.2.*",
            "1.0.0-beta.1",
            "1.0.0+build.42",
        ],
    )
    def test_accepts_valid_ranges(self, spec):
        assert is_semver_range(spec)

    @pytest.mark.parametrize(
        "spec",
        [
            "main",
            "develop",
            "abc123d",
            "abc123def4567",
            "latest",
            "",
            "v1@bad",
            "not-a-version",
            "@invalid",
            "v1.0.0",
            "1.2",
            "1",
            "^1.2",
            "~1.2",
            ">=1",
            "<2",
            ">=1,<2",
            "1.x",
            "1.*",
            "*",
        ],
    )
    def test_rejects_invalid_refs(self, spec):
        assert not is_semver_range(spec)


class TestMatchVersion:
    """Range-matching semantics."""

    @pytest.mark.parametrize(
        "spec,version",
        [
            ("^1.2.0", "1.2.0"),
            ("^1.2.0", "1.5.0"),
            ("^1.2.0", "1.99.99"),
            ("^0.2.3", "0.2.3"),
            ("^0.2.3", "0.2.99"),
            ("~1.2.3", "1.2.3"),
            ("~1.2.3", "1.2.99"),
            ("1.2.x", "1.2.0"),
            ("1.2.x", "1.2.99"),
            (">=1.0.0 <2.0.0", "1.99.0"),
            (">=1.0.0", "1.0.0"),
            ("1.0.0", "1.0.0"),
            ("1.0.0-beta.1", "1.0.0-beta.1"),
        ],
    )
    def test_match(self, spec, version):
        assert match_version(spec, version)

    @pytest.mark.parametrize(
        "spec,version",
        [
            ("^1.2.0", "2.0.0"),
            ("^1.2.0", "1.1.99"),
            ("^0.2.3", "0.3.0"),
            ("^0.2.3", "0.2.2"),
            ("~1.2.3", "1.3.0"),
            ("1.2.x", "1.3.0"),
            (">=1.0.0 <2.0.0", "2.0.0"),
            ("1.0.0", "1.0.1"),
            ("1.0.0", "0.9.99"),
        ],
    )
    def test_no_match(self, spec, version):
        assert not match_version(spec, version)

    def test_invalid_spec_does_not_match(self):
        assert not match_version("main", "1.0.0")

    def test_invalid_version_does_not_match(self):
        assert not match_version("^1.0.0", "abc")


class TestPickBest:
    """``pick_best`` returns the highest matching version, or None."""

    def test_picks_highest_in_range(self):
        versions = ["1.0.0", "1.2.0", "1.5.3", "2.0.0"]
        assert pick_best("^1.0.0", versions) == "1.5.3"

    def test_skips_outside_range(self):
        versions = ["1.0.0", "1.2.0", "2.0.0"]
        assert pick_best("~1.2.0", versions) == "1.2.0"

    def test_no_match_returns_none(self):
        assert pick_best(">=2.0.0", ["1.0.0"]) is None

    def test_invalid_spec_returns_none(self):
        assert pick_best("main", ["1.0.0"]) is None

    def test_skips_unparseable_versions(self):
        # An unparseable entry in the list is silently skipped, not raised.
        versions = ["1.0.0", "garbage", "1.5.0"]
        assert pick_best("^1.0.0", versions) == "1.5.0"

    def test_caret_zero_zero_x(self):
        """^0.0.x semantics: only the patch matches exactly."""
        assert pick_best("^0.0.3", ["0.0.3", "0.0.4"]) == "0.0.3"
        assert pick_best("^0.0.3", ["0.0.4", "0.0.5"]) is None

    def test_prerelease_sorts_before_release(self):
        versions = ["1.0.0-beta.1", "1.0.0"]
        assert pick_best(">=1.0.0-beta.1 <2.0.0", versions) == "1.0.0"
