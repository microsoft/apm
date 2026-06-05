"""Integration tests for apm outdated command coverage."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apm_cli.commands.outdated import (
    OutdatedRow,
    _find_remote_tip,
    _is_tag_ref,
    _strip_v,
)
from apm_cli.models.dependency.types import GitReferenceType


class RemoteRef:
    """Simple helper to represent a remote reference."""

    def __init__(self, ref_type, name, commit_sha):
        """Initialize a remote ref."""
        self.ref_type = ref_type
        self.name = name
        self.commit_sha = commit_sha


class TestIsTagRef:
    """Tests for _is_tag_ref helper."""

    def test_semver_with_v_prefix(self):
        """Recognizes v-prefixed semantic versions."""
        assert _is_tag_ref("v1.2.3") is True
        assert _is_tag_ref("v0.0.1") is True
        assert _is_tag_ref("v10.20.30") is True

    def test_semver_without_v_prefix(self):
        """Recognizes semantic versions without v prefix."""
        assert _is_tag_ref("1.2.3") is True
        assert _is_tag_ref("0.0.1") is True

    def test_non_semver_rejected(self):
        """Rejects non-semantic version strings."""
        assert _is_tag_ref("main") is False
        assert _is_tag_ref("master") is False
        assert _is_tag_ref("release-1") is False
        assert _is_tag_ref("v1") is False
        assert _is_tag_ref("v1.2") is False

    def test_name_underscore_v_version_pattern(self):
        """Recognizes ``{name}_v{version}`` style tags."""
        assert _is_tag_ref("api-governance_v1.0.1") is True
        assert _is_tag_ref("my-tool_v2.0.0") is True

    def test_name_at_version_not_recognized(self):
        """``@`` is marketplace install syntax, not inferred as a git tag."""
        assert _is_tag_ref("api-governance@1.0.1") is False

    def test_empty_string(self):
        """Empty string returns False."""
        assert _is_tag_ref("") is False

    def test_none_returns_false(self):
        """None input returns False."""
        assert _is_tag_ref(None) is False


class TestStripV:
    """Tests for _strip_v helper."""

    def test_strip_v_prefix(self):
        """Strips leading v from version strings."""
        assert _strip_v("v1.2.3") == "1.2.3"
        assert _strip_v("v0.0.1") == "0.0.1"

    def test_no_v_prefix(self):
        """Returns original string when no v prefix."""
        assert _strip_v("1.2.3") == "1.2.3"
        assert _strip_v("main") == "main"

    def test_empty_string(self):
        """Empty string returns empty string."""
        assert _strip_v("") == ""

    def test_none_returns_empty(self):
        """None returns empty string."""
        assert _strip_v(None) == ""

    def test_multiple_v_prefixes(self):
        """Only strips first v prefix."""
        assert _strip_v("vv1.2.3") == "v1.2.3"


class TestFindRemoteTip:
    """Tests for _find_remote_tip helper."""

    def test_find_explicit_branch_ref(self):
        """Finds commit SHA for explicit branch name."""
        remote_refs = [
            RemoteRef(
                ref_type=GitReferenceType.BRANCH,
                name="main",
                commit_sha="abc123",
            ),
            RemoteRef(
                ref_type=GitReferenceType.BRANCH,
                name="develop",
                commit_sha="def456",
            ),
        ]

        result = _find_remote_tip("main", remote_refs)

        assert result == "abc123"

    def test_find_default_main_branch(self):
        """Falls back to main branch when ref_name is None."""
        remote_refs = [
            RemoteRef(
                ref_type=GitReferenceType.BRANCH,
                name="main",
                commit_sha="abc123",
            ),
            RemoteRef(
                ref_type=GitReferenceType.BRANCH,
                name="develop",
                commit_sha="def456",
            ),
        ]

        result = _find_remote_tip(None, remote_refs)

        assert result == "abc123"

    def test_find_default_master_branch(self):
        """Falls back to master branch when main not found."""
        remote_refs = [
            RemoteRef(
                ref_type=GitReferenceType.BRANCH,
                name="master",
                commit_sha="abc123",
            ),
            RemoteRef(
                ref_type=GitReferenceType.BRANCH,
                name="develop",
                commit_sha="def456",
            ),
        ]

        result = _find_remote_tip(None, remote_refs)

        assert result == "abc123"

    def test_find_first_branch_as_last_resort(self):
        """Uses first branch when no default found."""
        remote_refs = [
            RemoteRef(
                ref_type=GitReferenceType.BRANCH,
                name="custom-branch",
                commit_sha="xyz789",
            ),
            RemoteRef(
                ref_type=GitReferenceType.BRANCH,
                name="another-branch",
                commit_sha="uvw456",
            ),
        ]

        result = _find_remote_tip(None, remote_refs)

        assert result == "xyz789"

    def test_ignores_tag_refs(self):
        """Only considers branch refs, ignores tags."""
        remote_refs = [
            RemoteRef(
                ref_type=GitReferenceType.TAG,
                name="v1.0.0",
                commit_sha="abc123",
            ),
            RemoteRef(
                ref_type=GitReferenceType.BRANCH,
                name="main",
                commit_sha="def456",
            ),
        ]

        result = _find_remote_tip("main", remote_refs)

        assert result == "def456"

    def test_empty_remote_refs(self):
        """Returns None when no remote refs available."""
        result = _find_remote_tip("main", [])

        assert result is None

    def test_missing_ref_in_remote_refs(self):
        """Returns None when requested ref not in remote refs."""
        remote_refs = [
            MagicMock(
                ref_type=GitReferenceType.BRANCH,
                name="develop",
                commit_sha="abc123",
            ),
        ]

        result = _find_remote_tip("nonexistent", remote_refs)

        assert result is None

    def test_none_remote_refs(self):
        """Returns None when remote refs is None."""
        result = _find_remote_tip("main", None)

        assert result is None


class TestOutdatedRow:
    """Tests for OutdatedRow dataclass."""

    def test_create_basic_row(self):
        """Create OutdatedRow with required fields."""
        row = OutdatedRow(
            package="owner/skill-repo",
            current="main",
            latest="abc123",
            status="up-to-date",
        )

        assert row.package == "owner/skill-repo"
        assert row.current == "main"
        assert row.latest == "abc123"
        assert row.status == "up-to-date"
        assert row.extra_tags == []
        assert row.source == ""

    def test_create_row_with_extra_tags(self):
        """Create OutdatedRow with extra_tags."""
        row = OutdatedRow(
            package="owner/skill-repo",
            current="main",
            latest="abc123",
            status="outdated",
            extra_tags=["v1.2.3", "v1.2.2"],
        )

        assert row.extra_tags == ["v1.2.3", "v1.2.2"]

    def test_create_row_with_source(self):
        """Create OutdatedRow with source."""
        row = OutdatedRow(
            package="plugin@marketplace",
            current="1.0.0",
            latest="1.1.0",
            status="outdated",
            source="marketplace: example",
        )

        assert row.source == "marketplace: example"

    def test_row_is_frozen(self):
        """OutdatedRow is immutable (frozen dataclass)."""
        row = OutdatedRow(
            package="owner/skill-repo",
            current="main",
            latest="abc123",
            status="up-to-date",
        )

        with pytest.raises(AttributeError):
            row.package = "other/package"

    def test_row_defaults(self):
        """OutdatedRow applies field defaults correctly."""
        row = OutdatedRow(
            package="owner/skill-repo",
            current="main",
            latest="abc123",
            status="up-to-date",
        )

        assert row.extra_tags == []
        assert row.source == ""


class TestOutdatedCommand:
    """Tests for outdated command structure."""

    def test_outdated_command_exists(self):
        """outdated command is available."""
        from apm_cli.commands.outdated import outdated

        assert outdated is not None

    def test_tag_re_pattern(self):
        """TAG_RE pattern matches semantic versions."""
        from apm_cli.commands.outdated import TAG_RE

        assert TAG_RE.match("v1.2.3") is not None
        assert TAG_RE.match("1.2.3") is not None
        assert TAG_RE.match("v1") is None
        assert TAG_RE.match("main") is None
