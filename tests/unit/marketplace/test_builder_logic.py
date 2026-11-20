"""Tests for builder.py -- MarketplaceBuilder, composition, diff, atomic write (pure logic cases)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional  # noqa: F401, UP035

import pytest

from apm_cli.marketplace.builder import (
    BuildReport,
)
from apm_cli.marketplace.errors import (
    BuildError,  # noqa: F401
)
from apm_cli.marketplace.semver import (
    SemVer,  # noqa: F401
    parse_semver,
    satisfies_range,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Standard marketplace.yml for many tests


# ---------------------------------------------------------------------------
# parse_semver
# ---------------------------------------------------------------------------


class TestParseSemver:
    """Tests for internal semver parser."""

    def test_basic(self) -> None:
        sv = parse_semver("1.2.3")
        assert sv is not None
        assert (sv.major, sv.minor, sv.patch) == (1, 2, 3)
        assert sv.prerelease == ""
        assert not sv.is_prerelease

    def test_full(self) -> None:
        sv = parse_semver("1.0.0-rc.1+build.5")
        assert sv is not None
        assert sv.prerelease == "rc.1"
        assert sv.build_meta == "build.5"

    def test_invalid(self) -> None:
        assert parse_semver("not-a-version") is None
        assert parse_semver("1.2") is None
        assert parse_semver("") is None


class TestSemverComparison:
    """Tests for SemVer ordering."""

    def test_basic_order(self) -> None:
        assert parse_semver("1.0.0") < parse_semver("2.0.0")  # type: ignore[operator]
        assert parse_semver("1.0.0") < parse_semver("1.1.0")  # type: ignore[operator]
        assert parse_semver("1.0.0") < parse_semver("1.0.1")  # type: ignore[operator]


# ---------------------------------------------------------------------------
# satisfies_range
# ---------------------------------------------------------------------------


class TestSatisfiesRange:
    """Tests for semver range matching."""

    def test_wildcard_star(self) -> None:
        assert satisfies_range(parse_semver("1.2.0"), "1.2.*")  # type: ignore[arg-type]

    def test_combined_range(self) -> None:
        """Space-separated constraints are AND-ed."""
        assert satisfies_range(parse_semver("1.5.0"), ">=1.0.0 <2.0.0")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("2.0.0"), ">=1.0.0 <2.0.0")  # type: ignore[arg-type]

    def test_empty_range(self) -> None:
        assert satisfies_range(parse_semver("1.0.0"), "")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Builder -- happy path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# APM-only field stripping
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Explicit ref pinning
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Prerelease handling
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tag pattern override
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Monorepo name-scoped tag pattern (issue #1822)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# No match error
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# continue_on_error
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Diff classification
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Owner optional fields
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Source composition (subdir -> path)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Deterministic output (round-trip)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Golden file
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# compose_marketplace_json direct tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# JSON formatting
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Empty packages list
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Duplicate package name warnings
# ---------------------------------------------------------------------------


class TestDuplicateNameWarnings:
    """Tests for defence-in-depth duplicate name detection in the builder."""

    def test_empty_build_report_primary_output_is_safe(self) -> None:
        report = BuildReport(outputs=())

        primary = report.primary_output

        assert primary.profile == ""
        assert primary.resolved == ()
        assert primary.errors == ()
        assert primary.warnings == ()
        assert primary.diagnostics == ()
        assert primary.output_path == Path(".")
        assert primary.unchanged_count == 0
        assert primary.added_count == 0
        assert primary.updated_count == 0
        assert primary.removed_count == 0


# ---------------------------------------------------------------------------
# _fetch_remote_metadata tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _fetch_local_metadata tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# compose_marketplace_json metadata enrichment tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auth token resolution tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _fetch_remote_metadata: GHE / custom host branching
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _ensure_auth lazy resolution
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# New fields & override semantics tests (#1061)
# ---------------------------------------------------------------------------
