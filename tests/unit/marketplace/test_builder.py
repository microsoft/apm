"""Tests for builder.py -- MarketplaceBuilder, composition, diff, atomic write."""

from __future__ import annotations

import json
import textwrap
import urllib.parse
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional  # noqa: F401, UP035
from unittest.mock import patch

import pytest

from apm_cli.marketplace.builder import (
    BuildOptions,
    BuildReport,
    MarketplaceBuilder,
    ResolvedPackage,
)
from apm_cli.marketplace.errors import (
    BuildError,  # noqa: F401
    HeadNotAllowedError,
    NoMatchingVersionError,
    RefNotFoundError,
)
from apm_cli.marketplace.ref_resolver import RemoteRef
from apm_cli.marketplace.semver import (
    SemVer,  # noqa: F401
    parse_semver,
    satisfies_range,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40
_SHA_D = "d" * 40

_GOLDEN_PATH = (
    Path(__file__).resolve().parent.parent.parent / "fixtures" / "marketplace" / "golden.json"
)

# Standard marketplace.yml for many tests
_BASIC_YML = """\
name: acme-tools
description: Curated developer tools by Acme Corp
version: 1.0.0
owner:
  name: Acme Corp
  email: tools@acme.example.com
  url: https://acme.example.com
metadata:
  pluginRoot: plugins
  category: developer-tools
packages:
  - name: code-reviewer
    source: acme/code-reviewer
    version: "^2.0.0"
    description: Automated code review assistant
    tags: [review, quality]
  - name: test-generator
    source: acme/test-generator
    version: "~1.0.0"
    subdir: src/plugin
    tags: [testing]
"""


def _write_yml(tmp_path: Path, content: str) -> Path:
    """Write content to marketplace.yml and return the path."""
    p = tmp_path / "marketplace.yml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def _make_refs(*tags: str, branches: list[str] | None = None) -> list[RemoteRef]:
    """Build a list of RemoteRef for testing.

    Tags are assigned SHAs starting from 'a' * 40, 'b' * 40, etc.
    """
    sha_chars = "abcdef0123456789"
    refs: list[RemoteRef] = []
    for i, tag in enumerate(tags):
        ch = sha_chars[i % len(sha_chars)]
        refs.append(RemoteRef(name=f"refs/tags/{tag}", sha=ch * 40))
    if branches:
        for i, branch in enumerate(branches):
            ch = sha_chars[(len(tags) + i) % len(sha_chars)]
            refs.append(RemoteRef(name=f"refs/heads/{branch}", sha=ch * 40))
    return refs


class _MockRefResolver:
    """In-process mock for RefResolver -- no subprocess calls."""

    def __init__(self, refs_by_remote: dict[str, list[RemoteRef]] | None = None):
        self._refs = refs_by_remote or {}

    def list_remote_refs(self, owner_repo: str) -> list[RemoteRef]:
        if owner_repo not in self._refs:
            from apm_cli.marketplace.errors import GitLsRemoteError

            raise GitLsRemoteError(
                package="",
                summary=f"Remote '{owner_repo}' not found.",
                hint="Check the source.",
            )
        return self._refs[owner_repo]

    def close(self) -> None:
        pass


def _build_with_mock(
    tmp_path: Path,
    yml_content: str,
    refs_by_remote: dict[str, list[RemoteRef]],
    options: BuildOptions | None = None,
) -> BuildReport:
    """Build using a mock ref resolver.

    Uses offline=True by default to prevent network calls during tests.
    """
    yml_path = _write_yml(tmp_path, yml_content)
    opts = options or BuildOptions(offline=True)
    builder = MarketplaceBuilder(yml_path, opts)
    builder._resolver = _MockRefResolver(refs_by_remote)  # type: ignore[assignment]
    return builder.build()


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

    def test_prerelease_less_than_release(self) -> None:
        assert parse_semver("1.0.0-alpha") < parse_semver("1.0.0")  # type: ignore[operator]

    def test_prerelease_ordering(self) -> None:
        assert parse_semver("1.0.0-alpha") < parse_semver("1.0.0-beta")  # type: ignore[operator]

    def test_equality(self) -> None:
        assert parse_semver("1.0.0") == parse_semver("1.0.0")


# ---------------------------------------------------------------------------
# satisfies_range
# ---------------------------------------------------------------------------


class TestSatisfiesRange:
    """Tests for semver range matching."""

    def test_exact(self) -> None:
        sv = parse_semver("1.2.3")
        assert sv is not None
        assert satisfies_range(sv, "1.2.3")
        assert not satisfies_range(sv, "1.2.4")

    def test_caret_major(self) -> None:
        """^1.2.3 := >=1.2.3, <2.0.0"""
        assert satisfies_range(parse_semver("1.2.3"), "^1.2.3")  # type: ignore[arg-type]
        assert satisfies_range(parse_semver("1.9.9"), "^1.2.3")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("2.0.0"), "^1.2.3")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("1.2.2"), "^1.2.3")  # type: ignore[arg-type]

    def test_caret_zero_minor(self) -> None:
        """^0.2.3 := >=0.2.3, <0.3.0"""
        assert satisfies_range(parse_semver("0.2.3"), "^0.2.3")  # type: ignore[arg-type]
        assert satisfies_range(parse_semver("0.2.9"), "^0.2.3")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("0.3.0"), "^0.2.3")  # type: ignore[arg-type]

    def test_caret_zero_zero(self) -> None:
        """^0.0.3 := >=0.0.3, <0.0.4"""
        assert satisfies_range(parse_semver("0.0.3"), "^0.0.3")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("0.0.4"), "^0.0.3")  # type: ignore[arg-type]

    def test_tilde(self) -> None:
        """~1.2.3 := >=1.2.3, <1.3.0"""
        assert satisfies_range(parse_semver("1.2.3"), "~1.2.3")  # type: ignore[arg-type]
        assert satisfies_range(parse_semver("1.2.9"), "~1.2.3")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("1.3.0"), "~1.2.3")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("1.2.2"), "~1.2.3")  # type: ignore[arg-type]

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

    def test_wildcard_x(self) -> None:
        assert satisfies_range(parse_semver("1.2.0"), "1.2.x")  # type: ignore[arg-type]
        assert satisfies_range(parse_semver("1.2.9"), "1.2.x")  # type: ignore[arg-type]
        assert not satisfies_range(parse_semver("1.3.0"), "1.2.x")  # type: ignore[arg-type]

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


class TestBuilderHappyPath:
    """Builder integration tests with mock ref resolver."""

    def test_basic_build(self, tmp_path: Path) -> None:
        refs = {
            "acme/code-reviewer": _make_refs("v2.0.0", "v2.1.0", "v1.0.0"),
            "acme/test-generator": _make_refs("v1.0.0", "v1.0.3", "v1.0.1"),
        }
        report = _build_with_mock(tmp_path, _BASIC_YML, refs)
        assert len(report.resolved) == 2
        assert report.resolved[0].name == "code-reviewer"
        assert report.resolved[0].ref == "v2.1.0"
        assert report.resolved[1].name == "test-generator"
        assert report.resolved[1].ref == "v1.0.3"

    def test_output_file_written(self, tmp_path: Path) -> None:
        refs = {
            "acme/code-reviewer": _make_refs("v2.0.0", "v2.1.0"),
            "acme/test-generator": _make_refs("v1.0.0", "v1.0.3"),
        }
        report = _build_with_mock(tmp_path, _BASIC_YML, refs)
        assert report.output_path.exists()
        data = json.loads(report.output_path.read_text("utf-8"))
        assert "plugins" in data
        assert len(data["plugins"]) == 2

    def test_plugin_order_matches_yml(self, tmp_path: Path) -> None:
        refs = {
            "acme/code-reviewer": _make_refs("v2.0.0"),
            "acme/test-generator": _make_refs("v1.0.0"),
        }
        report = _build_with_mock(tmp_path, _BASIC_YML, refs)
        assert report.resolved[0].name == "code-reviewer"
        assert report.resolved[1].name == "test-generator"

    def test_metadata_passthrough(self, tmp_path: Path) -> None:
        refs = {
            "acme/code-reviewer": _make_refs("v2.0.0"),
            "acme/test-generator": _make_refs("v1.0.0"),
        }
        report = _build_with_mock(tmp_path, _BASIC_YML, refs)
        data = json.loads(report.output_path.read_text("utf-8"))
        assert data["metadata"] == {"pluginRoot": "plugins", "category": "developer-tools"}

    def test_metadata_unusual_keys(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test marketplace
        version: 1.0.0
        owner:
          name: Test Owner
        metadata:
          pluginRoot: my-plugins
          customKey_123: some-value
          UPPER_CASE: yes
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
        """
        refs = {"acme/pkg1": _make_refs("v1.0.0")}
        report = _build_with_mock(tmp_path, yml, refs)
        data = json.loads(report.output_path.read_text("utf-8"))
        assert data["metadata"]["customKey_123"] == "some-value"
        assert data["metadata"]["UPPER_CASE"] is True

    def test_no_metadata_omitted(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test marketplace
        version: 1.0.0
        owner:
          name: Test Owner
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
        """
        refs = {"acme/pkg1": _make_refs("v1.0.0")}
        report = _build_with_mock(tmp_path, yml, refs)
        data = json.loads(report.output_path.read_text("utf-8"))
        assert "metadata" not in data

    def test_description_omitted_when_not_set(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test marketplace
        version: 1.0.0
        owner:
          name: Test Owner
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
        """
        refs = {"acme/pkg1": _make_refs("v1.0.0")}
        report = _build_with_mock(tmp_path, yml, refs)
        data = json.loads(report.output_path.read_text("utf-8"))
        assert "description" not in data["plugins"][0]


# ---------------------------------------------------------------------------
# APM-only field stripping
# ---------------------------------------------------------------------------


class TestFieldStripping:
    """Verify APM-only fields are stripped from output."""

    _APM_ONLY_KEYS = {"version", "ref", "subdir", "tag_pattern", "include_prerelease", "build"}  # noqa: RUF012

    def test_no_apm_keys_in_top_level(self, tmp_path: Path) -> None:
        refs = {
            "acme/code-reviewer": _make_refs("v2.0.0"),
            "acme/test-generator": _make_refs("v1.0.0"),
        }
        report = _build_with_mock(tmp_path, _BASIC_YML, refs)
        data = json.loads(report.output_path.read_text("utf-8"))
        assert "build" not in data

    def test_no_apm_keys_in_plugins(self, tmp_path: Path) -> None:
        refs = {
            "acme/code-reviewer": _make_refs("v2.0.0"),
            "acme/test-generator": _make_refs("v1.0.0"),
        }
        report = _build_with_mock(tmp_path, _BASIC_YML, refs)
        data = json.loads(report.output_path.read_text("utf-8"))
        for plugin in data["plugins"]:
            for key in self._APM_ONLY_KEYS:
                assert key not in plugin, f"APM-only key '{key}' found in plugin"

    def test_source_has_no_apm_keys(self, tmp_path: Path) -> None:
        refs = {
            "acme/code-reviewer": _make_refs("v2.0.0"),
            "acme/test-generator": _make_refs("v1.0.0"),
        }
        report = _build_with_mock(tmp_path, _BASIC_YML, refs)
        data = json.loads(report.output_path.read_text("utf-8"))
        for plugin in data["plugins"]:
            src = plugin["source"]
            assert "subdir" not in src
            assert "tag_pattern" not in src
            assert "include_prerelease" not in src


# ---------------------------------------------------------------------------
# Explicit ref pinning
# ---------------------------------------------------------------------------


class TestExplicitRef:
    """Tests for entries using ``ref:`` instead of ``version:``."""

    def test_tag_ref(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pinned
            source: acme/pinned
            ref: v3.0.0
        """
        refs = {"acme/pinned": _make_refs("v3.0.0", "v2.0.0")}
        report = _build_with_mock(tmp_path, yml, refs)
        assert report.resolved[0].ref == "v3.0.0"
        assert report.resolved[0].sha == "a" * 40

    def test_sha_ref(self, tmp_path: Path) -> None:
        sha = "a" * 40
        yml = f"""\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: sha-pinned
            source: acme/pinned
            ref: "{sha}"
        """
        refs = {"acme/pinned": _make_refs("v1.0.0")}
        report = _build_with_mock(tmp_path, yml, refs)
        assert report.resolved[0].sha == sha

    def test_branch_ref_rejected_without_allow_head(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: branched
            source: acme/branched
            ref: main
        """
        refs = {"acme/branched": _make_refs("v1.0.0", branches=["main"])}
        with pytest.raises(HeadNotAllowedError):
            _build_with_mock(tmp_path, yml, refs)

    def test_branch_ref_allowed_with_flag(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: branched
            source: acme/branched
            ref: main
        """
        refs = {"acme/branched": _make_refs("v1.0.0", branches=["main"])}
        opts = BuildOptions(allow_head=True)
        report = _build_with_mock(tmp_path, yml, refs, options=opts)
        assert report.resolved[0].ref == "main"

    def test_ref_not_found_raises(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: missing
            source: acme/missing
            ref: v99.0.0
        """
        refs = {"acme/missing": _make_refs("v1.0.0")}
        with pytest.raises(RefNotFoundError):
            _build_with_mock(tmp_path, yml, refs)


# ---------------------------------------------------------------------------
# Prerelease handling
# ---------------------------------------------------------------------------


class TestPrerelease:
    """Tests for prerelease inclusion/exclusion."""

    def test_prerelease_excluded_by_default(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg
            source: acme/pkg
            version: "^1.0.0"
        """
        refs = {"acme/pkg": _make_refs("v1.0.0", "v1.1.0-beta.1", "v1.0.1")}
        report = _build_with_mock(tmp_path, yml, refs)
        assert report.resolved[0].ref == "v1.0.1"
        assert not report.resolved[0].is_prerelease

    def test_prerelease_included_per_entry(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg
            source: acme/pkg
            version: "^1.0.0"
            include_prerelease: true
        """
        refs = {"acme/pkg": _make_refs("v1.0.0", "v1.1.0-beta.1", "v1.0.1")}
        report = _build_with_mock(tmp_path, yml, refs)
        # v1.1.0-beta.1 is highest matching ^1.0.0
        assert report.resolved[0].ref == "v1.1.0-beta.1"
        assert report.resolved[0].is_prerelease

    def test_prerelease_included_via_global_option(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg
            source: acme/pkg
            version: "^1.0.0"
        """
        refs = {"acme/pkg": _make_refs("v1.0.0", "v1.1.0-beta.1", "v1.0.1")}
        opts = BuildOptions(include_prerelease=True)
        report = _build_with_mock(tmp_path, yml, refs, options=opts)
        assert report.resolved[0].ref == "v1.1.0-beta.1"


# ---------------------------------------------------------------------------
# Tag pattern override
# ---------------------------------------------------------------------------


class TestTagPatternOverride:
    """Tests for tag pattern precedence."""

    def test_entry_pattern_wins(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        build:
          tagPattern: "v{version}"
        packages:
          - name: pkg
            source: acme/pkg
            version: "^1.0.0"
            tag_pattern: "release-{version}"
        """
        refs = {"acme/pkg": _make_refs("v1.0.0", "release-1.0.0", "release-1.1.0")}
        report = _build_with_mock(tmp_path, yml, refs)
        assert report.resolved[0].ref == "release-1.1.0"

    def test_build_pattern_fallback(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        build:
          tagPattern: "release-{version}"
        packages:
          - name: pkg
            source: acme/pkg
            version: "^1.0.0"
        """
        refs = {"acme/pkg": _make_refs("v1.0.0", "release-1.0.0", "release-1.1.0")}
        report = _build_with_mock(tmp_path, yml, refs)
        assert report.resolved[0].ref == "release-1.1.0"


# ---------------------------------------------------------------------------
# No match error
# ---------------------------------------------------------------------------


class TestNoMatch:
    """Tests for version range producing no candidates."""

    def test_no_matching_version(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg
            source: acme/pkg
            version: "^5.0.0"
        """
        refs = {"acme/pkg": _make_refs("v1.0.0", "v2.0.0")}
        with pytest.raises(NoMatchingVersionError, match="5.0.0"):  # noqa: RUF043
            _build_with_mock(tmp_path, yml, refs)


# ---------------------------------------------------------------------------
# continue_on_error
# ---------------------------------------------------------------------------


class TestContinueOnError:
    """Tests for --continue-on-error behaviour."""

    def test_errors_collected(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: good
            source: acme/good
            version: "^1.0.0"
          - name: bad
            source: acme/bad
            version: "^99.0.0"
        """
        refs = {
            "acme/good": _make_refs("v1.0.0"),
            "acme/bad": _make_refs("v1.0.0"),
        }
        opts = BuildOptions(continue_on_error=True)
        report = _build_with_mock(tmp_path, yml, refs, options=opts)
        assert len(report.resolved) == 1
        assert len(report.errors) == 1
        assert report.errors[0][0] == "bad"


# ---------------------------------------------------------------------------
# Diff classification
# ---------------------------------------------------------------------------


class TestDiffClassification:
    """Tests for the diff logic (added, updated, unchanged, removed)."""

    def test_first_build_all_added(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
        """
        refs = {"acme/pkg1": _make_refs("v1.0.0")}
        report = _build_with_mock(tmp_path, yml, refs)
        assert report.added_count == 1
        assert report.unchanged_count == 0
        assert report.updated_count == 0
        assert report.removed_count == 0

    def test_unchanged_on_rebuild(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
        """
        refs = {"acme/pkg1": _make_refs("v1.0.0")}
        # First build
        _build_with_mock(tmp_path, yml, refs)
        # Second build -- same refs
        report = _build_with_mock(tmp_path, yml, refs)
        assert report.unchanged_count == 1
        assert report.added_count == 0

    def test_updated_on_sha_change(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
        """
        refs_v1 = {"acme/pkg1": _make_refs("v1.0.0")}
        _build_with_mock(tmp_path, yml, refs_v1)
        # Now add v1.1.0 (different SHA)
        refs_v2 = {"acme/pkg1": _make_refs("v1.0.0", "v1.1.0")}
        report = _build_with_mock(tmp_path, yml, refs_v2)
        assert report.updated_count == 1

    def test_removed_on_package_drop(self, tmp_path: Path) -> None:
        yml_with = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
          - name: pkg2
            source: acme/pkg2
            version: "^1.0.0"
        """
        yml_without = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
        """
        refs = {
            "acme/pkg1": _make_refs("v1.0.0"),
            "acme/pkg2": _make_refs("v1.0.0"),
        }
        _build_with_mock(tmp_path, yml_with, refs)
        report = _build_with_mock(tmp_path, yml_without, refs)
        assert report.removed_count == 1
        assert report.unchanged_count == 1


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    """Tests for dry-run mode."""

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
        """
        refs = {"acme/pkg1": _make_refs("v1.0.0")}
        opts = BuildOptions(dry_run=True)
        report = _build_with_mock(tmp_path, yml, refs, options=opts)
        assert report.dry_run is True
        assert not report.output_path.exists()

    def test_dry_run_still_produces_report(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
        """
        refs = {"acme/pkg1": _make_refs("v1.0.0")}
        opts = BuildOptions(dry_run=True)
        report = _build_with_mock(tmp_path, yml, refs, options=opts)
        assert len(report.resolved) == 1


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    """Tests for atomic file writing."""

    def test_atomic_write_creates_file(self, tmp_path: Path) -> None:
        path = tmp_path / "test.json"
        MarketplaceBuilder._atomic_write(path, '{"hello": "world"}\n')
        assert path.exists()
        assert json.loads(path.read_text("utf-8")) == {"hello": "world"}

    def test_atomic_write_replaces_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "test.json"
        path.write_text('{"old": true}\n', encoding="utf-8")
        MarketplaceBuilder._atomic_write(path, '{"new": true}\n')
        assert json.loads(path.read_text("utf-8")) == {"new": True}

    def test_no_tmp_file_left(self, tmp_path: Path) -> None:
        path = tmp_path / "test.json"
        MarketplaceBuilder._atomic_write(path, '{"ok": true}\n')
        tmp_file = path.with_suffix(path.suffix + ".tmp")
        assert not tmp_file.exists()


# ---------------------------------------------------------------------------
# Owner optional fields
# ---------------------------------------------------------------------------


class TestOwnerFields:
    """Tests for owner field omission."""

    def test_owner_email_omitted_when_empty(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test Owner
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
        """
        refs = {"acme/pkg1": _make_refs("v1.0.0")}
        report = _build_with_mock(tmp_path, yml, refs)
        data = json.loads(report.output_path.read_text("utf-8"))
        assert "email" not in data["owner"]
        assert "url" not in data["owner"]

    def test_owner_full(self, tmp_path: Path) -> None:
        refs = {
            "acme/code-reviewer": _make_refs("v2.0.0"),
            "acme/test-generator": _make_refs("v1.0.0"),
        }
        report = _build_with_mock(tmp_path, _BASIC_YML, refs)
        data = json.loads(report.output_path.read_text("utf-8"))
        assert data["owner"]["email"] == "tools@acme.example.com"
        assert data["owner"]["url"] == "https://acme.example.com"


# ---------------------------------------------------------------------------
# Source composition (subdir -> path)
# ---------------------------------------------------------------------------


class TestSourceComposition:
    """Tests for the source object in plugins."""

    def test_subdir_becomes_path(self, tmp_path: Path) -> None:
        refs = {
            "acme/code-reviewer": _make_refs("v2.0.0"),
            "acme/test-generator": _make_refs("v1.0.0"),
        }
        report = _build_with_mock(tmp_path, _BASIC_YML, refs)
        data = json.loads(report.output_path.read_text("utf-8"))
        tg = data["plugins"][1]
        assert tg["source"]["path"] == "src/plugin"

    def test_no_subdir_no_path(self, tmp_path: Path) -> None:
        refs = {
            "acme/code-reviewer": _make_refs("v2.0.0"),
            "acme/test-generator": _make_refs("v1.0.0"),
        }
        report = _build_with_mock(tmp_path, _BASIC_YML, refs)
        data = json.loads(report.output_path.read_text("utf-8"))
        cr = data["plugins"][0]
        assert "path" not in cr["source"]


# ---------------------------------------------------------------------------
# Deterministic output (round-trip)
# ---------------------------------------------------------------------------


class TestDeterministicOutput:
    """Verify that same inputs produce byte-identical output."""

    def test_round_trip(self, tmp_path: Path) -> None:
        refs = {
            "acme/code-reviewer": _make_refs("v2.0.0"),
            "acme/test-generator": _make_refs("v1.0.0"),
        }
        # First build
        _build_with_mock(tmp_path, _BASIC_YML, refs)
        content1 = (tmp_path / "marketplace.json").read_bytes()

        # Second build (overwrite)
        _build_with_mock(tmp_path, _BASIC_YML, refs)
        content2 = (tmp_path / "marketplace.json").read_bytes()

        assert content1 == content2

    def test_json_key_order(self, tmp_path: Path) -> None:
        """Top-level keys appear in the documented order."""
        refs = {
            "acme/code-reviewer": _make_refs("v2.0.0"),
            "acme/test-generator": _make_refs("v1.0.0"),
        }
        report = _build_with_mock(tmp_path, _BASIC_YML, refs)
        data = json.loads(
            report.output_path.read_text("utf-8"),
            object_pairs_hook=OrderedDict,
        )
        keys = list(data.keys())
        assert keys == ["name", "description", "version", "owner", "metadata", "plugins"]


# ---------------------------------------------------------------------------
# Golden file
# ---------------------------------------------------------------------------


class TestGoldenFile:
    """Tests using the golden fixture file."""

    def test_golden_file_exists_and_parses(self) -> None:
        assert _GOLDEN_PATH.exists(), f"Golden file not found: {_GOLDEN_PATH}"
        data = json.loads(_GOLDEN_PATH.read_text("utf-8"))
        assert "name" in data
        assert "plugins" in data
        assert isinstance(data["plugins"], list)

    def test_golden_file_top_level_shape(self) -> None:
        data = json.loads(_GOLDEN_PATH.read_text("utf-8"))
        assert isinstance(data["name"], str)
        assert isinstance(data["description"], str)
        assert isinstance(data["version"], str)
        assert isinstance(data["owner"], dict)
        assert "name" in data["owner"]

    def test_golden_file_plugin_shape(self) -> None:
        data = json.loads(_GOLDEN_PATH.read_text("utf-8"))
        for plugin in data["plugins"]:
            assert "name" in plugin
            assert "tags" in plugin
            assert "source" in plugin
            src = plugin["source"]
            assert src["type"] == "github"
            assert "repository" in src
            assert "ref" in src
            assert "commit" in src

    def test_golden_file_no_apm_keys(self) -> None:
        data = json.loads(_GOLDEN_PATH.read_text("utf-8"))
        assert "build" not in data
        for plugin in data["plugins"]:
            assert "subdir" not in plugin
            assert "tag_pattern" not in plugin
            assert "include_prerelease" not in plugin

    def test_golden_file_trailing_newline(self) -> None:
        text = _GOLDEN_PATH.read_text("utf-8")
        assert text.endswith("\n")
        assert not text.endswith("\n\n")


# ---------------------------------------------------------------------------
# compose_marketplace_json direct tests
# ---------------------------------------------------------------------------


class TestComposeMarketplaceJson:
    """Direct tests for the composition method."""

    def test_compose_returns_ordered_dict(self, tmp_path: Path) -> None:
        yml_path = _write_yml(tmp_path, _BASIC_YML)
        builder = MarketplaceBuilder(yml_path, BuildOptions(offline=True))
        resolved = [
            ResolvedPackage(
                name="test-pkg",
                source_repo="acme/test-pkg",
                subdir=None,
                ref="v1.0.0",
                sha=_SHA_A,
                requested_version="^1.0.0",
                tags=("testing",),
                is_prerelease=False,
            ),
        ]
        result = builder.compose_marketplace_json(resolved)
        assert isinstance(result, OrderedDict)
        assert result["name"] == "acme-tools"
        assert result["plugins"][0]["source"]["source"] == "github"
        assert result["plugins"][0]["source"]["repo"] == "acme/test-pkg"

    def test_empty_packages(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        """
        yml_path = _write_yml(tmp_path, yml)
        builder = MarketplaceBuilder(yml_path, BuildOptions(offline=True))
        result = builder.compose_marketplace_json([])
        assert result["plugins"] == []


# ---------------------------------------------------------------------------
# Output override
# ---------------------------------------------------------------------------


class TestOutputOverride:
    """Tests for --output flag."""

    def test_custom_output_path(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
        """
        refs = {"acme/pkg1": _make_refs("v1.0.0")}
        custom_out = tmp_path / "custom" / "output.json"
        opts = BuildOptions(output_override=custom_out)
        report = _build_with_mock(tmp_path, yml, refs, options=opts)
        assert report.output_path == custom_out
        assert custom_out.exists()


# ---------------------------------------------------------------------------
# JSON formatting
# ---------------------------------------------------------------------------


class TestJsonFormatting:
    """Tests for JSON serialization rules."""

    def test_two_space_indent(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
        """
        refs = {"acme/pkg1": _make_refs("v1.0.0")}
        report = _build_with_mock(tmp_path, yml, refs)
        text = report.output_path.read_text("utf-8")
        # Check indentation: second line should start with 2 spaces
        lines = text.split("\n")
        assert lines[1].startswith("  ")

    def test_trailing_newline(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg1
            source: acme/pkg1
            version: "^1.0.0"
        """
        refs = {"acme/pkg1": _make_refs("v1.0.0")}
        report = _build_with_mock(tmp_path, yml, refs)
        text = report.output_path.read_text("utf-8")
        assert text.endswith("\n")


# ---------------------------------------------------------------------------
# Empty packages list
# ---------------------------------------------------------------------------


class TestEmptyPackages:
    """Tests for marketplace with no packages."""

    def test_empty_packages_produces_empty_plugins(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages: []
        """
        report = _build_with_mock(tmp_path, yml, {})
        assert len(report.resolved) == 0
        data = json.loads(report.output_path.read_text("utf-8"))
        assert data["plugins"] == []


# ---------------------------------------------------------------------------
# Duplicate package name warnings
# ---------------------------------------------------------------------------


class TestDuplicateNameWarnings:
    """Tests for defence-in-depth duplicate name detection in the builder."""

    def test_no_warnings_when_names_unique(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: pkg-alpha
            source: acme/pkg-alpha
            version: "^1.0.0"
          - name: pkg-beta
            source: acme/pkg-beta
            version: "^1.0.0"
        """
        refs = {
            "acme/pkg-alpha": _make_refs("v1.0.0"),
            "acme/pkg-beta": _make_refs("v1.0.0"),
        }
        report = _build_with_mock(tmp_path, yml, refs)
        assert report.warnings == ()

    def test_duplicate_names_produce_warning(self, tmp_path: Path) -> None:
        """Bypass yml_schema by feeding resolved packages directly."""
        yml_path = _write_yml(
            tmp_path,
            """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: alpha
            source: acme/alpha
            version: "^1.0.0"
        """,
        )
        refs = {"acme/alpha": _make_refs("v1.0.0")}
        builder = MarketplaceBuilder(yml_path, BuildOptions(offline=True))
        builder._resolver = _MockRefResolver(refs)  # type: ignore[assignment]

        # Craft two resolved packages with the same name but different paths.
        dupes = [
            ResolvedPackage(
                name="learning",
                source_repo="acme/repo",
                subdir="general/learning",
                ref="v1.0.0",
                sha=_SHA_A,
                requested_version="^1.0.0",
                tags=(),
                is_prerelease=False,
            ),
            ResolvedPackage(
                name="learning",
                source_repo="acme/repo",
                subdir="special/learning",
                ref="v1.0.0",
                sha=_SHA_B,
                requested_version="^1.0.0",
                tags=(),
                is_prerelease=False,
            ),
        ]
        builder.compose_marketplace_json(dupes)
        warnings = getattr(builder, "_compose_warnings", ())
        assert len(warnings) == 1
        assert "Duplicate package name 'learning'" in warnings[0]
        assert "general/learning" in warnings[0]
        assert "special/learning" in warnings[0]

    def test_duplicate_names_without_subdir_uses_repository(
        self,
        tmp_path: Path,
    ) -> None:
        """When subdir is absent, the warning should reference the repository."""
        yml_path = _write_yml(
            tmp_path,
            """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: alpha
            source: acme/alpha
            version: "^1.0.0"
        """,
        )
        builder = MarketplaceBuilder(yml_path, BuildOptions(offline=True))
        builder._resolver = _MockRefResolver(
            {  # type: ignore[assignment]
                "acme/alpha": _make_refs("v1.0.0"),
            }
        )

        dupes = [
            ResolvedPackage(
                name="tool",
                source_repo="acme/tool-a",
                subdir=None,
                ref="v1.0.0",
                sha=_SHA_A,
                requested_version="^1.0.0",
                tags=(),
                is_prerelease=False,
            ),
            ResolvedPackage(
                name="tool",
                source_repo="acme/tool-b",
                subdir=None,
                ref="v1.0.0",
                sha=_SHA_B,
                requested_version="^1.0.0",
                tags=(),
                is_prerelease=False,
            ),
        ]
        builder.compose_marketplace_json(dupes)
        warnings = getattr(builder, "_compose_warnings", ())
        assert len(warnings) == 1
        assert "acme/tool-a" in warnings[0]
        assert "acme/tool-b" in warnings[0]

    def test_build_report_carries_warnings(self, tmp_path: Path) -> None:
        """BuildReport.warnings is empty for a clean build."""
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        packages:
          - name: solo
            source: acme/solo
            version: "^1.0.0"
        """
        refs = {"acme/solo": _make_refs("v1.0.0")}
        report = _build_with_mock(tmp_path, yml, refs)
        assert isinstance(report.warnings, tuple)
        assert len(report.warnings) == 0


# ---------------------------------------------------------------------------
# _fetch_remote_metadata tests
# ---------------------------------------------------------------------------


class TestFetchRemoteMetadata:
    """Tests for best-effort remote apm.yml metadata fetching."""

    def _make_pkg(
        self,
        *,
        name: str = "my-tool",
        source_repo: str = "acme/my-tool",
        subdir: str | None = None,
        sha: str = _SHA_A,
    ) -> ResolvedPackage:
        return ResolvedPackage(
            name=name,
            source_repo=source_repo,
            subdir=subdir,
            ref="v1.0.0",
            sha=sha,
            requested_version="^1.0.0",
            tags=("testing",),
            is_prerelease=False,
        )

    def _make_builder(self, tmp_path: Path) -> MarketplaceBuilder:
        yml_path = _write_yml(tmp_path, _BASIC_YML)
        return MarketplaceBuilder(yml_path)

    def test_happy_path_returns_description_and_version(self, tmp_path: Path) -> None:
        """urlopen returns valid YAML with description and version."""
        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        yaml_body = b"name: my-tool\ndescription: Remote tool description\nversion: 2.3.1\n"
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch("apm_cli.marketplace.builder.urllib.request.urlopen", return_value=mock_resp):
            result = builder._fetch_remote_metadata(pkg)
        assert result is not None
        assert result["description"] == "Remote tool description"
        assert result["version"] == "2.3.1"

    def test_happy_path_with_subdir(self, tmp_path: Path) -> None:
        """URL includes subdir when present on the package."""
        pkg = self._make_pkg(subdir="src/plugin")
        builder = self._make_builder(tmp_path)
        yaml_body = b"description: Nested plugin desc\nversion: 1.0.0\n"
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch(
            "apm_cli.marketplace.builder.urllib.request.urlopen",
            return_value=mock_resp,
        ) as mock_open:
            result = builder._fetch_remote_metadata(pkg)
        assert result is not None
        assert result["description"] == "Nested plugin desc"
        # Verify the URL contains the subdir
        call_args = mock_open.call_args
        req = call_args[0][0]
        assert "src/plugin/apm.yml" in req.full_url

    def test_description_only(self, tmp_path: Path) -> None:
        """YAML with description but no version."""
        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        yaml_body = b"name: my-tool\ndescription: Only desc\n"
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch("apm_cli.marketplace.builder.urllib.request.urlopen", return_value=mock_resp):
            result = builder._fetch_remote_metadata(pkg)
        assert result is not None
        assert result["description"] == "Only desc"
        assert "version" not in result

    def test_version_only(self, tmp_path: Path) -> None:
        """YAML with version but no description."""
        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        yaml_body = b"name: my-tool\nversion: 3.0.0\n"
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch("apm_cli.marketplace.builder.urllib.request.urlopen", return_value=mock_resp):
            result = builder._fetch_remote_metadata(pkg)
        assert result is not None
        assert result["version"] == "3.0.0"
        assert "description" not in result

    def test_network_failure_returns_none(self, tmp_path: Path) -> None:
        """URLError from urlopen -> returns None, no crash."""
        import urllib.error

        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        with patch(
            "apm_cli.marketplace.builder.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = builder._fetch_remote_metadata(pkg)
        assert result is None

    def test_http_404_returns_none(self, tmp_path: Path) -> None:
        """HTTP 404 -> returns None."""
        import urllib.error

        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        with patch(
            "apm_cli.marketplace.builder.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                url="",
                code=404,
                msg="Not Found",
                hdrs=None,
                fp=None,  # type: ignore[arg-type]
            ),
        ):
            result = builder._fetch_remote_metadata(pkg)
        assert result is None

    def test_no_description_no_version_returns_none(self, tmp_path: Path) -> None:
        """YAML without description or version -> returns None."""
        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        yaml_body = b"name: my-tool\ntags:\n  - util\n"
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch("apm_cli.marketplace.builder.urllib.request.urlopen", return_value=mock_resp):
            result = builder._fetch_remote_metadata(pkg)
        assert result is None

    def test_empty_description_excluded(self, tmp_path: Path) -> None:
        """YAML with empty description string -> excluded from result."""
        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        yaml_body = b'name: my-tool\ndescription: ""\nversion: 1.0.0\n'
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch("apm_cli.marketplace.builder.urllib.request.urlopen", return_value=mock_resp):
            result = builder._fetch_remote_metadata(pkg)
        assert result is not None
        assert "description" not in result
        assert result["version"] == "1.0.0"

    def test_non_dict_yaml_returns_none(self, tmp_path: Path) -> None:
        """YAML that parses to a non-dict (e.g. a list) -> returns None."""
        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        yaml_body = b"- item1\n- item2\n"
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch("apm_cli.marketplace.builder.urllib.request.urlopen", return_value=mock_resp):
            result = builder._fetch_remote_metadata(pkg)
        assert result is None

    def test_invalid_yaml_returns_none(self, tmp_path: Path) -> None:
        """Unparseable YAML -> returns None."""
        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        yaml_body = b"{{{{not: yaml at all"
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch("apm_cli.marketplace.builder.urllib.request.urlopen", return_value=mock_resp):
            result = builder._fetch_remote_metadata(pkg)
        assert result is None

    def test_numeric_version_coerced_to_string(self, tmp_path: Path) -> None:
        """YAML version as float (e.g. 1.0) is coerced to string."""
        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        yaml_body = b"name: my-tool\nversion: 1.0\n"
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch("apm_cli.marketplace.builder.urllib.request.urlopen", return_value=mock_resp):
            result = builder._fetch_remote_metadata(pkg)
        assert result is not None
        assert result["version"] == "1.0"

    def test_auth_header_added_when_token_present(self, tmp_path: Path) -> None:
        """When _github_token is set, Authorization header is included."""
        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        builder._github_token = "ghp_faketoken123"
        yaml_body = b"description: Private plugin\nversion: 1.0.0\n"
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch(
            "apm_cli.marketplace.builder.urllib.request.urlopen",
            return_value=mock_resp,
        ) as mock_open:
            result = builder._fetch_remote_metadata(pkg)
        assert result is not None
        assert result["description"] == "Private plugin"
        # Verify Authorization header was set on the Request
        call_args = mock_open.call_args
        req = call_args[0][0]
        assert req.get_header("Authorization") == "token ghp_faketoken123"

    def test_no_auth_header_when_no_token(self, tmp_path: Path) -> None:
        """When _github_token is None, no Authorization header is set."""
        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        builder._github_token = None
        yaml_body = b"description: Public plugin\nversion: 2.0.0\n"
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch(
            "apm_cli.marketplace.builder.urllib.request.urlopen",
            return_value=mock_resp,
        ) as mock_open:
            result = builder._fetch_remote_metadata(pkg)
        assert result is not None
        call_args = mock_open.call_args
        req = call_args[0][0]
        assert req.get_header("Authorization") is None


class _FakeHTTPResponse:
    """Minimal file-like mock for urllib.request.urlopen return value."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args: object) -> None:
        pass


# ---------------------------------------------------------------------------
# compose_marketplace_json metadata enrichment tests
# ---------------------------------------------------------------------------


class TestMetadataEnrichment:
    """Tests for compose_marketplace_json remote metadata enrichment."""

    def test_enrichment_populates_description_and_version(self, tmp_path: Path) -> None:
        """Package gets description and version from remote fetch."""
        yml_path = _write_yml(tmp_path, _BASIC_YML)
        builder = MarketplaceBuilder(yml_path)
        resolved = [
            ResolvedPackage(
                name="enriched-pkg",
                source_repo="acme/enriched-pkg",
                subdir=None,
                ref="v1.0.0",
                sha=_SHA_A,
                requested_version="^1.0.0",
                tags=("test",),
                is_prerelease=False,
            ),
        ]
        with patch.object(
            MarketplaceBuilder,
            "_fetch_remote_metadata",
            return_value={"description": "Fetched desc", "version": "1.2.3"},
        ):
            result = builder.compose_marketplace_json(resolved)
        assert result["plugins"][0]["description"] == "Fetched desc"
        assert result["plugins"][0]["version"] == "1.2.3"

    def test_remote_fetch_failure_leaves_no_description_or_version(
        self,
        tmp_path: Path,
    ) -> None:
        """When remote fetch returns None, plugin has no description or version."""
        yml_path = _write_yml(tmp_path, _BASIC_YML)
        builder = MarketplaceBuilder(yml_path)
        resolved = [
            ResolvedPackage(
                name="fail-pkg",
                source_repo="acme/fail-pkg",
                subdir=None,
                ref="v1.0.0",
                sha=_SHA_A,
                requested_version="^1.0.0",
                tags=("test",),
                is_prerelease=False,
            ),
        ]
        with patch.object(
            MarketplaceBuilder,
            "_fetch_remote_metadata",
            return_value=None,
        ):
            result = builder.compose_marketplace_json(resolved)
        assert "description" not in result["plugins"][0]
        assert "version" not in result["plugins"][0]

    def test_offline_mode_skips_fetch(self, tmp_path: Path) -> None:
        """When offline=True, no remote fetch is attempted."""
        yml_path = _write_yml(tmp_path, _BASIC_YML)
        builder = MarketplaceBuilder(yml_path, BuildOptions(offline=True))
        resolved = [
            ResolvedPackage(
                name="offline-pkg",
                source_repo="acme/offline-pkg",
                subdir=None,
                ref="v1.0.0",
                sha=_SHA_A,
                requested_version="^1.0.0",
                tags=("test",),
                is_prerelease=False,
            ),
        ]
        with patch.object(
            MarketplaceBuilder,
            "_fetch_remote_metadata",
        ) as mock_fetch:
            result = builder.compose_marketplace_json(resolved)
        mock_fetch.assert_not_called()
        assert "description" not in result["plugins"][0]
        assert "version" not in result["plugins"][0]

    def test_partial_metadata_only_description(self, tmp_path: Path) -> None:
        """Remote returns only description, no version."""
        yml_path = _write_yml(tmp_path, _BASIC_YML)
        builder = MarketplaceBuilder(yml_path)
        resolved = [
            ResolvedPackage(
                name="desc-only-pkg",
                source_repo="acme/desc-only-pkg",
                subdir=None,
                ref="v1.0.0",
                sha=_SHA_A,
                requested_version="^1.0.0",
                tags=(),
                is_prerelease=False,
            ),
        ]
        with patch.object(
            MarketplaceBuilder,
            "_fetch_remote_metadata",
            return_value={"description": "Only desc"},
        ):
            result = builder.compose_marketplace_json(resolved)
        assert result["plugins"][0]["description"] == "Only desc"
        assert "version" not in result["plugins"][0]

    def test_partial_metadata_only_version(self, tmp_path: Path) -> None:
        """Remote returns only version, no description."""
        yml_path = _write_yml(tmp_path, _BASIC_YML)
        builder = MarketplaceBuilder(yml_path)
        resolved = [
            ResolvedPackage(
                name="ver-only-pkg",
                source_repo="acme/ver-only-pkg",
                subdir=None,
                ref="v1.0.0",
                sha=_SHA_A,
                requested_version="^1.0.0",
                tags=(),
                is_prerelease=False,
            ),
        ]
        with patch.object(
            MarketplaceBuilder,
            "_fetch_remote_metadata",
            return_value={"version": "4.5.6"},
        ):
            result = builder.compose_marketplace_json(resolved)
        assert "description" not in result["plugins"][0]
        assert result["plugins"][0]["version"] == "4.5.6"


# ---------------------------------------------------------------------------
# Auth token resolution tests
# ---------------------------------------------------------------------------


class TestResolveGitHubToken:
    """Tests for _resolve_github_token and auth integration in _prefetch_metadata."""

    def test_resolve_token_returns_token_from_auth_resolver(self, tmp_path: Path) -> None:
        """When AuthResolver returns a token, _resolve_github_token returns it."""
        from unittest.mock import MagicMock

        mock_resolver = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.token = "ghp_resolved_token"
        mock_ctx.source = "GITHUB_TOKEN"
        mock_resolver.resolve.return_value = mock_ctx

        yml_path = _write_yml(tmp_path, _BASIC_YML)
        builder = MarketplaceBuilder(yml_path, auth_resolver=mock_resolver)
        token = builder._resolve_github_token()
        assert token == "ghp_resolved_token"
        mock_resolver.resolve.assert_called_once_with("github.com")

    def test_resolve_token_returns_none_when_no_token(self, tmp_path: Path) -> None:
        """When AuthResolver returns no token, _resolve_github_token returns None."""
        from unittest.mock import MagicMock

        mock_resolver = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.token = None
        mock_resolver.resolve.return_value = mock_ctx

        yml_path = _write_yml(tmp_path, _BASIC_YML)
        builder = MarketplaceBuilder(yml_path, auth_resolver=mock_resolver)
        token = builder._resolve_github_token()
        assert token is None

    def test_resolve_token_returns_none_on_exception(self, tmp_path: Path) -> None:
        """When AuthResolver raises, _resolve_github_token returns None (best-effort)."""
        from unittest.mock import MagicMock

        mock_resolver = MagicMock()
        mock_resolver.resolve.side_effect = RuntimeError("auth explosion")

        yml_path = _write_yml(tmp_path, _BASIC_YML)
        builder = MarketplaceBuilder(yml_path, auth_resolver=mock_resolver)
        token = builder._resolve_github_token()
        assert token is None

    def test_resolve_token_lazy_creates_resolver(self, tmp_path: Path) -> None:
        """When no auth_resolver is provided, one is created lazily."""
        from unittest.mock import MagicMock

        yml_path = _write_yml(tmp_path, _BASIC_YML)
        builder = MarketplaceBuilder(yml_path)  # no auth_resolver
        assert builder._auth_resolver is None

        mock_ctx = MagicMock()
        mock_ctx.token = "ghp_lazy_token"
        mock_ctx.source = "GH_TOKEN"
        with patch("apm_cli.core.auth.AuthResolver") as MockAuthCls:
            MockAuthCls.return_value.resolve.return_value = mock_ctx
            token = builder._resolve_github_token()
        assert token == "ghp_lazy_token"
        assert builder._auth_resolver is not None

    def test_prefetch_metadata_resolves_token_before_fetching(
        self,
        tmp_path: Path,
    ) -> None:
        """_prefetch_metadata resolves the token once, then workers use it."""
        from unittest.mock import MagicMock

        mock_resolver = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.token = "ghp_prefetch_token"
        mock_ctx.source = "GITHUB_APM_PAT"
        mock_resolver.resolve.return_value = mock_ctx

        yml_path = _write_yml(tmp_path, _BASIC_YML)
        builder = MarketplaceBuilder(yml_path, auth_resolver=mock_resolver)
        resolved = [
            ResolvedPackage(
                name="auth-pkg",
                source_repo="acme/auth-pkg",
                subdir=None,
                ref="v1.0.0",
                sha=_SHA_A,
                requested_version="^1.0.0",
                tags=(),
                is_prerelease=False,
            ),
        ]
        yaml_body = b"description: Auth test\nversion: 1.0.0\n"
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch(
            "apm_cli.marketplace.builder.urllib.request.urlopen",
            return_value=mock_resp,
        ) as mock_open:
            results = builder._prefetch_metadata(resolved)
        # Token was resolved
        assert builder._github_token == "ghp_prefetch_token"
        # Request included auth header
        call_args = mock_open.call_args
        req = call_args[0][0]
        assert req.get_header("Authorization") == "token ghp_prefetch_token"
        # Result was populated
        assert "auth-pkg" in results
        assert results["auth-pkg"]["description"] == "Auth test"

    def test_prefetch_metadata_works_without_token(self, tmp_path: Path) -> None:
        """_prefetch_metadata works even when no token is available."""
        from unittest.mock import MagicMock

        mock_resolver = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.token = None
        mock_resolver.resolve.return_value = mock_ctx

        yml_path = _write_yml(tmp_path, _BASIC_YML)
        builder = MarketplaceBuilder(yml_path, auth_resolver=mock_resolver)
        resolved = [
            ResolvedPackage(
                name="public-pkg",
                source_repo="acme/public-pkg",
                subdir=None,
                ref="v1.0.0",
                sha=_SHA_A,
                requested_version="^1.0.0",
                tags=(),
                is_prerelease=False,
            ),
        ]
        yaml_body = b"description: Public test\nversion: 2.0.0\n"
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch(
            "apm_cli.marketplace.builder.urllib.request.urlopen",
            return_value=mock_resp,
        ) as mock_open:
            results = builder._prefetch_metadata(resolved)
        # No token was set
        assert builder._github_token is None
        # Request had no auth header
        call_args = mock_open.call_args
        req = call_args[0][0]
        assert req.get_header("Authorization") is None
        # Result was still populated (public repo)
        assert "public-pkg" in results


# ---------------------------------------------------------------------------
# _fetch_remote_metadata: GHE / custom host branching
# ---------------------------------------------------------------------------


class TestFetchRemoteMetadataGHEHost:
    """Tests for _fetch_remote_metadata host-routing logic (GHES, GHE Cloud, generic)."""

    def _make_pkg(
        self,
        *,
        name: str = "test-pkg",
        source_repo: str = "acme/tools",
        subdir: str | None = None,
        sha: str = _SHA_A,
    ) -> ResolvedPackage:
        return ResolvedPackage(
            name=name,
            source_repo=source_repo,
            subdir=subdir,
            ref="v1.0.0",
            sha=sha,
            requested_version="^1.0.0",
            tags=(),
            is_prerelease=False,
        )

    def _make_builder(self, tmp_path: Path) -> MarketplaceBuilder:
        return MarketplaceBuilder(_write_yml(tmp_path, _BASIC_YML))

    def test_metadata_fetch_ghes_uses_rest_api(self, tmp_path: Path) -> None:
        """GHES host triggers REST API URL and sets Accept: application/vnd.github.raw."""
        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        builder._host = "corp.ghe.com"
        builder._github_token = "test-token"
        builder._host_info = SimpleNamespace(
            kind="ghes",
            api_base="https://corp.ghe.com/api/v3",
        )
        yaml_body = b"description: GHES tool\nversion: 1.2.3\n"
        mock_resp = _FakeHTTPResponse(yaml_body)
        with patch(
            "apm_cli.marketplace.builder.urllib.request.urlopen",
            return_value=mock_resp,
        ) as mock_open:
            result = builder._fetch_remote_metadata(pkg)
        assert result is not None
        assert result["description"] == "GHES tool"
        req = mock_open.call_args[0][0]
        parsed = urllib.parse.urlparse(req.full_url)
        assert parsed.hostname == "corp.ghe.com"
        assert parsed.path.startswith("/api/v3/repos/")
        assert req.get_header("Accept") == "application/vnd.github.raw"

    def test_metadata_fetch_non_github_skipped(self, tmp_path: Path) -> None:
        """Non-GitHub host (kind='generic') returns None without any HTTP request."""
        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        builder._host = "gitlab.example.com"
        builder._host_info = SimpleNamespace(kind="generic", api_base=None)
        with patch(
            "apm_cli.marketplace.builder.urllib.request.urlopen",
        ) as mock_open:
            result = builder._fetch_remote_metadata(pkg)
        assert result is None
        mock_open.assert_not_called()

    def test_metadata_fetch_ghe_cloud_no_token_skipped(self, tmp_path: Path) -> None:
        """GHE Cloud host without a token returns None without any HTTP request."""
        pkg = self._make_pkg()
        builder = self._make_builder(tmp_path)
        builder._host = "mycompany.ghe.com"
        builder._github_token = None
        builder._host_info = SimpleNamespace(
            kind="ghe_cloud",
            api_base="https://mycompany.ghe.com/api/v3",
        )
        with patch(
            "apm_cli.marketplace.builder.urllib.request.urlopen",
        ) as mock_open:
            result = builder._fetch_remote_metadata(pkg)
        assert result is None
        mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# _ensure_auth lazy resolution
# ---------------------------------------------------------------------------


class TestEnsureAuth:
    """Tests for the lazy _ensure_auth() method."""

    def test_ensure_auth_populates_token(self, tmp_path: Path) -> None:
        """_ensure_auth() resolves token via the injected auth resolver."""
        yml_path = tmp_path / "marketplace.yml"
        yml_path.write_text("name: test\noutput: out.json\npackages: []\n")
        builder = MarketplaceBuilder(yml_path)

        mock_ctx = SimpleNamespace(token="ghp_resolved", source="env")
        # Pre-set _host_info so classify_host() branch is skipped,
        # and inject a fake auth resolver so AuthResolver() ctor is skipped.
        builder._host_info = SimpleNamespace(kind="github", api_base="https://api.github.com")
        builder._auth_resolver = SimpleNamespace(resolve=lambda host: mock_ctx)

        builder._ensure_auth()

        assert builder._github_token == "ghp_resolved"
        assert builder._host_info is not None

    def test_ensure_auth_skips_offline(self, tmp_path: Path) -> None:
        """_ensure_auth() short-circuits immediately in offline mode."""
        yml_path = tmp_path / "marketplace.yml"
        yml_path.write_text("name: test\noutput: out.json\npackages: []\n")
        builder = MarketplaceBuilder(yml_path, options=BuildOptions(offline=True))

        builder._ensure_auth()

        assert builder._github_token is None

    def test_ensure_auth_idempotent(self, tmp_path: Path) -> None:
        """Calling _ensure_auth() when already resolved does not re-resolve."""
        yml_path = tmp_path / "marketplace.yml"
        yml_path.write_text("name: test\noutput: out.json\npackages: []\n")
        builder = MarketplaceBuilder(yml_path)
        builder._github_token = "already_set"
        builder._auth_resolved = True

        with patch.object(builder, "_resolve_github_token") as mock_resolve:
            builder._ensure_auth()
            mock_resolve.assert_not_called()

        assert builder._github_token == "already_set"

    def test_get_resolver_has_token(self, tmp_path: Path) -> None:
        """_get_resolver() passes the resolved token to RefResolver."""
        yml_path = tmp_path / "marketplace.yml"
        yml_path.write_text("name: test\noutput: out.json\npackages: []\n")
        builder = MarketplaceBuilder(yml_path)

        mock_ctx = SimpleNamespace(token="ghp_wired", source="env")
        builder._host_info = SimpleNamespace(kind="github", api_base="https://api.github.com")
        builder._auth_resolver = SimpleNamespace(resolve=lambda host: mock_ctx)

        resolver = builder._get_resolver()
        assert resolver._token == "ghp_wired"


# ---------------------------------------------------------------------------
# New fields & override semantics tests (#1061)
# ---------------------------------------------------------------------------


class TestRemoteOverrideSemantics:
    """Entry-level description/version override remote-fetched values."""

    def _make_remote_builder(self, tmp_path, entry_fields=""):
        from apm_cli.marketplace.builder import BuildOptions, ResolvedPackage
        from apm_cli.marketplace.migration import load_marketplace_config

        content = textwrap.dedent(f"""\
            name: test
            description: x
            version: 1.0.0
            marketplace:
              owner:
                name: ACME
              packages:
                - name: remote-tool
                  source: acme/remote-tool
                  ref: v1.0.0
                  {entry_fields}
        """)
        (tmp_path / "apm.yml").write_text(content, encoding="utf-8")
        config = load_marketplace_config(tmp_path)
        builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
        resolved = [
            ResolvedPackage(
                name="remote-tool",
                source_repo="acme/remote-tool",
                subdir=None,
                ref="v1.0.0",
                sha="a" * 40,
                requested_version=None,
                tags=(),
                is_prerelease=False,
            )
        ]
        return builder, resolved

    def test_remote_entry_override_description_version(self, tmp_path):
        builder, resolved = self._make_remote_builder(
            tmp_path, 'description: "custom"\n                  version: "3.0.0"'
        )
        builder._prefetch_metadata = lambda r: {
            "remote-tool": {"description": "remote desc", "version": "1.0.0"}
        }
        doc = builder.compose_marketplace_json(resolved)
        plugin = doc["plugins"][0]
        assert plugin["description"] == "custom"
        assert plugin["version"] == "3.0.0"

    def test_remote_entry_no_override_uses_fetched(self, tmp_path):
        builder, resolved = self._make_remote_builder(tmp_path)
        builder._prefetch_metadata = lambda r: {
            "remote-tool": {"description": "remote desc", "version": "1.0.0"}
        }
        doc = builder.compose_marketplace_json(resolved)
        plugin = doc["plugins"][0]
        assert plugin["description"] == "remote desc"
        assert plugin["version"] == "1.0.0"

    def test_author_license_repository_emitted_for_local(self, tmp_path):
        from apm_cli.marketplace.builder import BuildOptions
        from apm_cli.marketplace.migration import load_marketplace_config

        content = textwrap.dedent("""\
            name: test
            description: x
            version: 1.0.0
            marketplace:
              owner:
                name: ACME
              packages:
                - name: local-tool
                  source: ./plugins/local-tool
                  author: "ACME Inc"
                  license: "MIT"
                  repository: "https://github.com/acme/tool"
        """)
        (tmp_path / "apm.yml").write_text(content, encoding="utf-8")
        config = load_marketplace_config(tmp_path)
        builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
        local_entry = next(e for e in config.packages if e.is_local)
        resolved = [builder._resolve_entry(local_entry)]
        doc = builder.compose_marketplace_json(resolved)
        plugin = doc["plugins"][0]
        # Per Claude Code plugin manifest schema, author must be an object.
        assert plugin["author"] == {"name": "ACME Inc"}
        assert plugin["license"] == "MIT"
        assert plugin["repository"] == "https://github.com/acme/tool"

    def test_author_license_repository_emitted_for_remote(self, tmp_path):
        builder, resolved = self._make_remote_builder(
            tmp_path,
            'author: "ACME"\n                  license: "Apache-2.0"\n                  repository: "https://github.com/acme/remote"',
        )
        doc = builder.compose_marketplace_json(resolved)
        plugin = doc["plugins"][0]
        assert plugin["author"] == {"name": "ACME"}
        assert plugin["license"] == "Apache-2.0"
        assert plugin["repository"] == "https://github.com/acme/remote"

    def test_author_object_form_preserved(self, tmp_path):
        builder, resolved = self._make_remote_builder(
            tmp_path,
            'author:\n                    name: "ACME"\n                    email: "team@acme.example"\n                    url: "https://acme.example"',
        )
        doc = builder.compose_marketplace_json(resolved)
        plugin = doc["plugins"][0]
        assert plugin["author"] == {
            "name": "ACME",
            "email": "team@acme.example",
            "url": "https://acme.example",
        }

    def test_serialization_order(self, tmp_path):
        from apm_cli.marketplace.builder import BuildOptions
        from apm_cli.marketplace.migration import load_marketplace_config

        content = textwrap.dedent("""\
            name: test
            description: x
            version: 1.0.0
            marketplace:
              owner:
                name: ACME
              packages:
                - name: local-tool
                  source: ./plugins/local-tool
                  description: "A tool"
                  version: "1.0.0"
                  author: "ACME"
                  license: "MIT"
                  repository: "https://github.com/acme/tool"
                  tags: [ai]
                  homepage: "https://acme.com"
        """)
        (tmp_path / "apm.yml").write_text(content, encoding="utf-8")
        config = load_marketplace_config(tmp_path)
        builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
        local_entry = next(e for e in config.packages if e.is_local)
        resolved = [builder._resolve_entry(local_entry)]
        doc = builder.compose_marketplace_json(resolved)
        plugin = doc["plugins"][0]
        keys = list(plugin.keys())
        # Expected order: name, description, version, author, license, repository, tags, homepage, source
        assert keys == [
            "name",
            "description",
            "version",
            "author",
            "license",
            "repository",
            "tags",
            "homepage",
            "source",
        ]
