"""Tests for builder.py -- MarketplaceBuilder, composition, diff, atomic write."""

from __future__ import annotations

import json
import textwrap
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from apm_cli.marketplace.builder import (
    BuildOptions,
    BuildReport,
    MarketplaceBuilder,
    ResolvedPackage,
)
from apm_cli.marketplace.semver import (
    SemVer,
    parse_semver,
    satisfies_range,
)
from apm_cli.marketplace.errors import (
    BuildError,
    HeadNotAllowedError,
    NoMatchingVersionError,
    RefNotFoundError,
)
from apm_cli.marketplace.ref_resolver import RemoteRef


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40
_SHA_D = "d" * 40

_GOLDEN_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "fixtures"
    / "marketplace"
    / "golden.json"
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


def _make_refs(*tags: str, branches: Optional[List[str]] = None) -> List[RemoteRef]:
    """Build a list of RemoteRef for testing.

    Tags are assigned SHAs starting from 'a' * 40, 'b' * 40, etc.
    """
    sha_chars = "abcdef0123456789"
    refs: List[RemoteRef] = []
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

    def __init__(self, refs_by_remote: Optional[Dict[str, List[RemoteRef]]] = None):
        self._refs = refs_by_remote or {}

    def list_remote_refs(self, owner_repo: str) -> List[RemoteRef]:
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
    refs_by_remote: Dict[str, List[RemoteRef]],
    options: Optional[BuildOptions] = None,
) -> BuildReport:
    """Build using a mock ref resolver."""
    yml_path = _write_yml(tmp_path, yml_content)
    opts = options or BuildOptions()
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

    _APM_ONLY_KEYS = {"version", "ref", "subdir", "tag_pattern", "include_prerelease", "build"}

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
        with pytest.raises(NoMatchingVersionError, match="5.0.0"):
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
            assert "version" not in plugin
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
        builder = MarketplaceBuilder(yml_path)
        resolved = [
            ResolvedPackage(
                name="test-pkg",
                source_repo="acme/test-pkg",
                subdir=None,
                ref="v1.0.0",
                sha=_SHA_A,
                requested_version="^1.0.0",
                description="A test package",
                tags=("testing",),
                is_prerelease=False,
            ),
        ]
        result = builder.compose_marketplace_json(resolved)
        assert isinstance(result, OrderedDict)
        assert result["name"] == "acme-tools"
        assert result["plugins"][0]["source"]["type"] == "github"

    def test_empty_packages(self, tmp_path: Path) -> None:
        yml = """\
        name: test-mkt
        description: Test
        version: 1.0.0
        owner:
          name: Test
        """
        yml_path = _write_yml(tmp_path, yml)
        builder = MarketplaceBuilder(yml_path)
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
