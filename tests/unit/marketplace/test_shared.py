"""Unit tests for apm_cli.marketplace._shared.

Covers ``iter_semver_tags``. parse_semver is mocked where it is looked
up (inside the ``_shared`` module) to avoid pulling in the real SemVer
implementation.  No I/O occurs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from unittest.mock import patch

from apm_cli.marketplace._shared import iter_semver_tags

# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


@dataclass
class FakeRef:
    """Minimal stand-in for a remote-ref object."""

    name: str
    sha: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CATCH_ALL_RX = re.compile(r"(?P<version>.+)")
NO_MATCH_RX = re.compile(r"^NEVER_MATCHES_(?P<version>.+)$")

# parse_semver is imported inside iter_semver_tags at call-time via
# ``from .semver import parse_semver``, so it must be patched at the source.
_PARSE_SEMVER = "apm_cli.marketplace.semver.parse_semver"


class _FakeSemVer:
    """Trivial stand-in for SemVer used in positive-path tests."""

    def __init__(self, raw: str) -> None:
        self.raw = raw

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeSemVer) and self.raw == other.raw


# ---------------------------------------------------------------------------
# iter_semver_tags
# ---------------------------------------------------------------------------


class TestIterSemverTagsEmptyRefs:
    def test_empty_refs_yields_nothing(self) -> None:
        with patch(_PARSE_SEMVER, return_value=_FakeSemVer("1.0.0")):
            result = list(iter_semver_tags([], CATCH_ALL_RX))
        assert result == []


class TestIterSemverTagsFiltering:
    def test_non_tag_ref_is_skipped(self) -> None:
        """Refs not starting with refs/tags/ must be ignored."""
        refs = [
            FakeRef(name="refs/heads/main", sha="sha1"),
            FakeRef(name="refs/pull/1/head", sha="sha2"),
        ]
        with patch(_PARSE_SEMVER, return_value=_FakeSemVer("1.0.0")) as mock_ps:
            result = list(iter_semver_tags(refs, CATCH_ALL_RX))
        assert result == []
        mock_ps.assert_not_called()

    def test_tag_not_matching_regex_is_skipped(self) -> None:
        refs = [FakeRef(name="refs/tags/v1.0.0", sha="sha1")]
        with patch(_PARSE_SEMVER, return_value=_FakeSemVer("1.0.0")) as mock_ps:
            result = list(iter_semver_tags(refs, NO_MATCH_RX))
        assert result == []
        mock_ps.assert_not_called()

    def test_tag_matching_but_parse_semver_returns_none_is_skipped(self) -> None:
        refs = [FakeRef(name="refs/tags/not-a-version", sha="sha1")]
        with patch(_PARSE_SEMVER, return_value=None):
            result = list(iter_semver_tags(refs, CATCH_ALL_RX))
        assert result == []

    def test_tag_with_specific_regex_not_matching_is_skipped(self) -> None:
        version_only_rx = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
        refs = [FakeRef(name="refs/tags/release-1.0.0", sha="sha1")]
        with patch(_PARSE_SEMVER, return_value=_FakeSemVer("1.0.0")) as mock_ps:
            result = list(iter_semver_tags(refs, version_only_rx))
        assert result == []
        mock_ps.assert_not_called()


class TestIterSemverTagsYields:
    def test_single_valid_tag_yields_triple(self) -> None:
        sv = _FakeSemVer("1.0.0")
        refs = [FakeRef(name="refs/tags/v1.0.0", sha="deadbeef")]
        with patch(_PARSE_SEMVER, return_value=sv):
            result = list(iter_semver_tags(refs, CATCH_ALL_RX))
        assert len(result) == 1
        yielded_sv, tag_name, sha = result[0]
        assert yielded_sv is sv
        assert tag_name == "v1.0.0"
        assert sha == "deadbeef"

    def test_sha_comes_from_ref_sha_attribute(self) -> None:
        """Ensure .sha (not .commit_sha or any other attr) is used."""
        sv = _FakeSemVer("2.0.0")
        refs = [FakeRef(name="refs/tags/v2.0.0", sha="cafebabe")]
        with patch(_PARSE_SEMVER, return_value=sv):
            result = list(iter_semver_tags(refs, CATCH_ALL_RX))
        assert result[0][2] == "cafebabe"

    def test_multiple_valid_tags_all_yielded(self) -> None:
        semver_objects = [_FakeSemVer("1.0.0"), _FakeSemVer("2.0.0"), _FakeSemVer("3.0.0")]
        refs = [
            FakeRef(name="refs/tags/v1.0.0", sha="sha1"),
            FakeRef(name="refs/tags/v2.0.0", sha="sha2"),
            FakeRef(name="refs/tags/v3.0.0", sha="sha3"),
        ]
        with patch(_PARSE_SEMVER, side_effect=semver_objects):
            result = list(iter_semver_tags(refs, CATCH_ALL_RX))
        assert len(result) == 3
        assert result[0][1] == "v1.0.0"
        assert result[1][1] == "v2.0.0"
        assert result[2][1] == "v3.0.0"

    def test_version_group_extracted_from_regex(self) -> None:
        """parse_semver must be called with the captured ``version`` group."""
        prefix_rx = re.compile(r"^pkg-(?P<version>.+)$")
        refs = [FakeRef(name="refs/tags/pkg-1.2.3", sha="abc")]
        with patch(_PARSE_SEMVER, return_value=_FakeSemVer("1.2.3")) as mock_ps:
            list(iter_semver_tags(refs, prefix_rx))
        mock_ps.assert_called_once_with("1.2.3")

    def test_mixed_refs_only_valid_tags_yielded(self) -> None:
        sv = _FakeSemVer("1.0.0")
        refs = [
            FakeRef(name="refs/heads/main", sha="sha0"),
            FakeRef(name="refs/tags/v1.0.0", sha="sha1"),
            FakeRef(name="refs/tags/not-a-version", sha="sha2"),
        ]
        call_count = 0

        def fake_parse(s: str) -> _FakeSemVer | None:
            nonlocal call_count
            call_count += 1
            return sv if s == "v1.0.0" else None

        with patch(_PARSE_SEMVER, side_effect=fake_parse):
            result = list(iter_semver_tags(refs, CATCH_ALL_RX))

        assert len(result) == 1
        assert result[0][1] == "v1.0.0"
        # parse_semver was called for both tags (non-head refs matching regex)
        assert call_count == 2

    def test_tag_name_stripped_of_refs_tags_prefix(self) -> None:
        """The yielded tag_name must have the refs/tags/ prefix removed."""
        sv = _FakeSemVer("1.0.0")
        refs = [FakeRef(name="refs/tags/v1.0.0", sha="abc")]
        with patch(_PARSE_SEMVER, return_value=sv):
            result = list(iter_semver_tags(refs, CATCH_ALL_RX))
        assert result[0][1] == "v1.0.0"
        assert not result[0][1].startswith("refs/tags/")
