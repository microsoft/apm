"""Unit tests for apm_cli.deps.git_remote_ops.

Covers:
- parse_ls_remote_output
- semver_sort_key
- sort_remote_refs

No I/O or network calls; all input is plain strings / data objects.
"""

from __future__ import annotations

from apm_cli.deps.git_remote_ops import (
    parse_ls_remote_output,
    semver_sort_key,
    sort_remote_refs,
)
from apm_cli.models.apm_package import GitReferenceType, RemoteRef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _branch(name: str, sha: str = "aabbccdd") -> RemoteRef:
    return RemoteRef(name=name, ref_type=GitReferenceType.BRANCH, commit_sha=sha)


def _tag(name: str, sha: str = "aabbccdd") -> RemoteRef:
    return RemoteRef(name=name, ref_type=GitReferenceType.TAG, commit_sha=sha)


# ---------------------------------------------------------------------------
# parse_ls_remote_output
# ---------------------------------------------------------------------------


class TestParseLsRemoteOutput:
    def test_empty_string_returns_empty_list(self) -> None:
        assert parse_ls_remote_output("") == []

    def test_blank_lines_are_skipped(self) -> None:
        output = "\n\n   \n"
        assert parse_ls_remote_output(output) == []

    def test_line_without_tab_is_skipped(self) -> None:
        output = "abc123 refs/heads/main"
        assert parse_ls_remote_output(output) == []

    def test_single_branch(self) -> None:
        output = "abc123\trefs/heads/main"
        refs = parse_ls_remote_output(output)
        assert len(refs) == 1
        ref = refs[0]
        assert ref.name == "main"
        assert ref.ref_type == GitReferenceType.BRANCH
        assert ref.commit_sha == "abc123"

    def test_multiple_branches(self) -> None:
        output = "sha1\trefs/heads/main\nsha2\trefs/heads/feature/x\n"
        refs = parse_ls_remote_output(output)
        branches = [r for r in refs if r.ref_type == GitReferenceType.BRANCH]
        names = {r.name for r in branches}
        assert names == {"main", "feature/x"}

    def test_simple_tag(self) -> None:
        output = "abc123\trefs/tags/v1.0.0"
        refs = parse_ls_remote_output(output)
        assert len(refs) == 1
        ref = refs[0]
        assert ref.name == "v1.0.0"
        assert ref.ref_type == GitReferenceType.TAG
        assert ref.commit_sha == "abc123"

    def test_annotated_tag_deref_wins(self) -> None:
        """The ^{} dereferenced line must replace the tag-object SHA."""
        output = "sha1\trefs/tags/v1.0.0\nsha2\trefs/tags/v1.0.0^{}\n"
        refs = parse_ls_remote_output(output)
        tag_refs = [r for r in refs if r.ref_type == GitReferenceType.TAG]
        assert len(tag_refs) == 1
        assert tag_refs[0].commit_sha == "sha2"

    def test_deref_before_plain_line_deref_still_wins(self) -> None:
        """Even if ^{} comes first in output, it should be preferred (setdefault semantics)."""
        # In practice git always emits plain before ^{}, but let's also test
        # the case where the order is reversed to confirm setdefault behaviour.
        # Per the implementation: deref line overwrites; plain uses setdefault.
        # If deref arrives first (unusual), plain setdefault won't overwrite → deref sha kept.
        output = "sha_deref\trefs/tags/v2.0.0^{}\nsha_plain\trefs/tags/v2.0.0\n"
        refs = parse_ls_remote_output(output)
        tag_refs = [r for r in refs if r.ref_type == GitReferenceType.TAG]
        assert len(tag_refs) == 1
        # The deref line wrote "sha_deref"; setdefault on plain won't change it.
        assert tag_refs[0].commit_sha == "sha_deref"

    def test_tag_without_deref_sha_stored(self) -> None:
        output = "deadbeef\trefs/tags/v0.1.0\n"
        refs = parse_ls_remote_output(output)
        tag_refs = [r for r in refs if r.ref_type == GitReferenceType.TAG]
        assert len(tag_refs) == 1
        assert tag_refs[0].commit_sha == "deadbeef"

    def test_unknown_ref_format_is_skipped(self) -> None:
        output = "abc\trefs/pull/42/head\n"
        refs = parse_ls_remote_output(output)
        assert refs == []

    def test_mixed_tags_and_branches(self) -> None:
        output = "sha1\trefs/heads/main\nsha2\trefs/tags/v1.0.0\nsha3\trefs/heads/dev\n"
        refs = parse_ls_remote_output(output)
        branches = [r for r in refs if r.ref_type == GitReferenceType.BRANCH]
        tags = [r for r in refs if r.ref_type == GitReferenceType.TAG]
        assert {b.name for b in branches} == {"main", "dev"}
        assert len(tags) == 1 and tags[0].name == "v1.0.0"

    def test_whitespace_is_stripped_from_sha_and_refname(self) -> None:
        output = "  abc123  \t  refs/heads/main  "
        refs = parse_ls_remote_output(output)
        assert len(refs) == 1
        assert refs[0].commit_sha == "abc123"
        assert refs[0].name == "main"


# ---------------------------------------------------------------------------
# semver_sort_key
# ---------------------------------------------------------------------------


class TestSemverSortKey:
    def test_standard_version(self) -> None:
        assert semver_sort_key("v1.2.3") == (0, -1, -2, -3, "")

    def test_no_v_prefix(self) -> None:
        assert semver_sort_key("1.0.0") == (0, -1, 0, 0, "")

    def test_capital_v_prefix(self) -> None:
        assert semver_sort_key("V2.0.0") == (0, -2, 0, 0, "")

    def test_prerelease_suffix(self) -> None:
        assert semver_sort_key("1.0.0-alpha") == (0, -1, 0, 0, "-alpha")

    def test_non_semver_returns_fallback(self) -> None:
        assert semver_sort_key("not-semver") == (1, "not-semver")

    def test_major_only_non_semver(self) -> None:
        # "1.0" doesn't have three numeric groups
        key = semver_sort_key("1.0")
        assert key[0] == 1  # falls into the non-semver bucket

    def test_descending_order(self) -> None:
        """Higher semver version must yield a *lower* sort key (for ascending sort = descending version)."""
        key_v2 = semver_sort_key("v2.0.0")
        key_v1 = semver_sort_key("v1.0.0")
        assert key_v2 < key_v1, "v2.0.0 should sort before v1.0.0"

    def test_semver_before_non_semver(self) -> None:
        """All semver keys must sort before non-semver keys."""
        assert semver_sort_key("v1.0.0")[0] == 0
        assert semver_sort_key("some-branch")[0] == 1

    def test_patch_descending(self) -> None:
        key_high = semver_sort_key("v1.0.3")
        key_low = semver_sort_key("v1.0.1")
        assert key_high < key_low, "patch 3 should sort before patch 1"


# ---------------------------------------------------------------------------
# sort_remote_refs
# ---------------------------------------------------------------------------


class TestSortRemoteRefs:
    def test_empty_list(self) -> None:
        assert sort_remote_refs([]) == []

    def test_tags_before_branches(self) -> None:
        refs = [_branch("main"), _tag("v1.0.0")]
        result = sort_remote_refs(refs)
        types = [r.ref_type for r in result]
        tag_indices = [i for i, t in enumerate(types) if t == GitReferenceType.TAG]
        branch_indices = [i for i, t in enumerate(types) if t == GitReferenceType.BRANCH]
        assert max(tag_indices) < min(branch_indices)

    def test_tags_sorted_semver_descending(self) -> None:
        refs = [_tag("v1.0.0"), _tag("v2.0.0"), _tag("v1.5.0")]
        result = sort_remote_refs(refs)
        tag_names = [r.name for r in result if r.ref_type == GitReferenceType.TAG]
        assert tag_names == ["v2.0.0", "v1.5.0", "v1.0.0"]

    def test_branches_sorted_alphabetically(self) -> None:
        refs = [_branch("main"), _branch("alpha"), _branch("zeta"), _branch("beta")]
        result = sort_remote_refs(refs)
        branch_names = [r.name for r in result if r.ref_type == GitReferenceType.BRANCH]
        assert branch_names == sorted(["main", "alpha", "zeta", "beta"])

    def test_mixed_tags_and_branches_order(self) -> None:
        refs = [
            _branch("main"),
            _tag("v1.0.0"),
            _branch("alpha"),
            _tag("v2.0.0"),
        ]
        result = sort_remote_refs(refs)
        # All tags first (sorted desc), then all branches (sorted alpha)
        assert result[0].name == "v2.0.0"
        assert result[1].name == "v1.0.0"
        assert result[2].name == "alpha"
        assert result[3].name == "main"

    def test_only_branches(self) -> None:
        refs = [_branch("z"), _branch("a"), _branch("m")]
        result = sort_remote_refs(refs)
        assert [r.name for r in result] == ["a", "m", "z"]

    def test_only_tags(self) -> None:
        refs = [_tag("v3.0.0"), _tag("v1.0.0"), _tag("v2.0.0")]
        result = sort_remote_refs(refs)
        assert [r.name for r in result] == ["v3.0.0", "v2.0.0", "v1.0.0"]

    def test_non_semver_tags_after_semver_tags(self) -> None:
        refs = [_tag("not-semver"), _tag("v1.0.0"), _tag("also-not")]
        result = sort_remote_refs(refs)
        tag_names = [r.name for r in result if r.ref_type == GitReferenceType.TAG]
        # semver tags come first (bucket 0), non-semver last (bucket 1)
        assert tag_names[0] == "v1.0.0"
        # both non-semver entries follow
        assert set(tag_names[1:]) == {"not-semver", "also-not"}
