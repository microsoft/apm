"""Unit tests for apm_cli.models.dependency.types.

Covers:
- GitReferenceType enum values
- RemoteRef dataclass
- VirtualPackageType enum values
- ResolvedReference dataclass and __str__
- parse_git_reference function

No I/O; all tests are pure-Python.
"""

from __future__ import annotations

from apm_cli.models.dependency.types import (
    GitReferenceType,
    RemoteRef,
    ResolvedReference,
    VirtualPackageType,
    parse_git_reference,
)

# ---------------------------------------------------------------------------
# GitReferenceType
# ---------------------------------------------------------------------------


class TestGitReferenceType:
    def test_branch_value(self) -> None:
        assert GitReferenceType.BRANCH.value == "branch"

    def test_tag_value(self) -> None:
        assert GitReferenceType.TAG.value == "tag"

    def test_commit_value(self) -> None:
        assert GitReferenceType.COMMIT.value == "commit"

    def test_enum_has_three_members(self) -> None:
        assert len(GitReferenceType) == 3

    def test_members_are_distinct(self) -> None:
        members = list(GitReferenceType)
        assert len(set(members)) == len(members)


# ---------------------------------------------------------------------------
# RemoteRef
# ---------------------------------------------------------------------------


class TestRemoteRef:
    def test_instantiation_and_field_access(self) -> None:
        ref = RemoteRef(
            name="main",
            ref_type=GitReferenceType.BRANCH,
            commit_sha="abc1234",
        )
        assert ref.name == "main"
        assert ref.ref_type == GitReferenceType.BRANCH
        assert ref.commit_sha == "abc1234"

    def test_tag_ref(self) -> None:
        ref = RemoteRef(name="v1.0.0", ref_type=GitReferenceType.TAG, commit_sha="deadbeef")
        assert ref.ref_type == GitReferenceType.TAG
        assert ref.name == "v1.0.0"

    def test_equality(self) -> None:
        r1 = RemoteRef(name="main", ref_type=GitReferenceType.BRANCH, commit_sha="abc")
        r2 = RemoteRef(name="main", ref_type=GitReferenceType.BRANCH, commit_sha="abc")
        assert r1 == r2

    def test_inequality(self) -> None:
        r1 = RemoteRef(name="main", ref_type=GitReferenceType.BRANCH, commit_sha="abc")
        r2 = RemoteRef(name="dev", ref_type=GitReferenceType.BRANCH, commit_sha="abc")
        assert r1 != r2


# ---------------------------------------------------------------------------
# VirtualPackageType
# ---------------------------------------------------------------------------


class TestVirtualPackageType:
    def test_file_value(self) -> None:
        assert VirtualPackageType.FILE.value == "file"

    def test_subdirectory_value(self) -> None:
        assert VirtualPackageType.SUBDIRECTORY.value == "subdirectory"

    def test_enum_has_two_members(self) -> None:
        assert len(VirtualPackageType) == 2


# ---------------------------------------------------------------------------
# ResolvedReference.__str__
# ---------------------------------------------------------------------------


class TestResolvedReferenceStr:
    def test_no_resolved_commit_returns_ref_name(self) -> None:
        rr = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=None,
            ref_name="main",
        )
        assert str(rr) == "main"

    def test_empty_resolved_commit_returns_ref_name(self) -> None:
        rr = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="",
            ref_name="main",
        )
        assert str(rr) == "main"

    def test_commit_type_returns_first_8_chars(self) -> None:
        sha = "abcdef1234567890"
        rr = ResolvedReference(
            original_ref=sha,
            ref_type=GitReferenceType.COMMIT,
            resolved_commit=sha,
            ref_name=sha,
        )
        assert str(rr) == sha[:8]

    def test_branch_type_returns_name_and_short_sha(self) -> None:
        sha = "abcdef1234567890"
        rr = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=sha,
            ref_name="main",
        )
        assert str(rr) == f"main ({sha[:8]})"

    def test_tag_type_returns_name_and_short_sha(self) -> None:
        sha = "deadbeef12345678"
        rr = ResolvedReference(
            original_ref="v1.0.0",
            ref_type=GitReferenceType.TAG,
            resolved_commit=sha,
            ref_name="v1.0.0",
        )
        assert str(rr) == f"v1.0.0 ({sha[:8]})"

    def test_short_sha_exactly_8_chars(self) -> None:
        sha = "abcdef1234567890"
        rr = ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=sha,
            ref_name="main",
        )
        result = str(rr)
        # extract the sha portion inside parentheses
        inner = result.split("(")[1].rstrip(")")
        assert len(inner) == 8

    def test_defaults_for_optional_fields(self) -> None:
        """resolved_commit and ref_name have defaults."""
        rr = ResolvedReference(original_ref="main", ref_type=GitReferenceType.BRANCH)
        # resolved_commit is None by default → returns ref_name (empty string)
        assert str(rr) == ""


# ---------------------------------------------------------------------------
# parse_git_reference
# ---------------------------------------------------------------------------


class TestParseGitReference:
    def test_empty_string_defaults_to_main_branch(self) -> None:
        ref_type, ref = parse_git_reference("")
        assert ref_type == GitReferenceType.BRANCH
        assert ref == "main"

    def test_plain_branch_name(self) -> None:
        ref_type, ref = parse_git_reference("main")
        assert ref_type == GitReferenceType.BRANCH
        assert ref == "main"

    def test_feature_branch_with_slash(self) -> None:
        ref_type, ref = parse_git_reference("feature/test")
        assert ref_type == GitReferenceType.BRANCH
        assert ref == "feature/test"

    def test_seven_hex_chars_is_commit(self) -> None:
        ref_type, ref = parse_git_reference("abc1234")
        assert ref_type == GitReferenceType.COMMIT
        assert ref == "abc1234"

    def test_forty_hex_chars_is_commit(self) -> None:
        sha = "abcdef1234567890abcdef1234567890abcdef12"
        ref_type, ref = parse_git_reference(sha)
        assert ref_type == GitReferenceType.COMMIT
        assert ref == sha

    def test_semver_with_v_prefix_is_tag(self) -> None:
        ref_type, ref = parse_git_reference("v1.2.3")
        assert ref_type == GitReferenceType.TAG
        assert ref == "v1.2.3"

    def test_semver_without_v_prefix_is_tag(self) -> None:
        ref_type, ref = parse_git_reference("1.0.0")
        assert ref_type == GitReferenceType.TAG
        assert ref == "1.0.0"

    def test_semver_with_prerelease_is_tag(self) -> None:
        ref_type, ref = parse_git_reference("v1.2.3-beta")
        assert ref_type == GitReferenceType.TAG
        assert ref == "v1.2.3-beta"

    def test_uppercase_hex_seven_chars_is_commit(self) -> None:
        ref_type, ref = parse_git_reference("ABCDEF1")
        assert ref_type == GitReferenceType.COMMIT
        # The original string is returned (not lowercased)
        assert ref == "ABCDEF1"

    def test_leading_trailing_spaces_stripped(self) -> None:
        ref_type, ref = parse_git_reference("   main  ")
        assert ref_type == GitReferenceType.BRANCH
        assert ref == "main"

    def test_mixed_letters_digits_not_all_hex_is_branch(self) -> None:
        # "ghijkl7" contains non-hex letters → branch
        ref_type, _ref = parse_git_reference("ghijkl7")
        assert ref_type == GitReferenceType.BRANCH

    def test_six_hex_chars_is_branch(self) -> None:
        # Only 6 hex chars (below minimum of 7) → not a commit SHA
        ref_type, _ = parse_git_reference("abc123")
        assert ref_type == GitReferenceType.BRANCH

    def test_forty_one_hex_chars_is_branch(self) -> None:
        # Too long for a SHA → falls through to branch
        sha_plus = "a" * 41
        ref_type, _ = parse_git_reference(sha_plus)
        assert ref_type == GitReferenceType.BRANCH

    def test_semver_build_metadata_is_tag(self) -> None:
        ref_type, ref = parse_git_reference("v1.0.0+build.1")
        assert ref_type == GitReferenceType.TAG
        assert ref == "v1.0.0+build.1"

    def test_mixed_case_branch_name(self) -> None:
        ref_type, ref = parse_git_reference("Feature/MyBranch")
        assert ref_type == GitReferenceType.BRANCH
        assert ref == "Feature/MyBranch"
