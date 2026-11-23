"""A dependency ref resolves only to an exact branch or tag of its origin."""

import subprocess
from unittest.mock import patch

import pytest

from apm_cli.cache.git_cache import GitCache

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-git-ref-freshness")]

_URL = "https://github.com/acme/pkg.git"
_A, _B, _C, _D = "a" * 40, "b" * 40, "c" * 40, "d" * 40


def _listing(*rows):
    out = "".join(f"{sha}\t{ref}\n" for sha, ref in rows)
    return subprocess.CompletedProcess(["git"], 0, stdout=out, stderr="")


def _resolve(tmp_path, ref, rows):
    with patch("apm_cli.utils.git_env.git_remote_refs", return_value=_listing(*rows)):
        return GitCache(tmp_path, refresh=False)._ls_remote_resolve(_URL, ref)


@pytest.mark.parametrize("ref", ["head", "pull/4917/head", "100/head", "merge"])
def test_a_ref_naming_no_branch_or_tag_never_resolves_to_a_review_head(tmp_path, ref):
    rows = [
        (_A, "refs/pull/100/head"),
        (_B, "refs/pull/4917/head"),
        (_C, "refs/pull/4917/merge"),
    ]
    with pytest.raises(RuntimeError, match="branch or tag"):
        _resolve(tmp_path, ref, rows)


@pytest.mark.parametrize(
    ("ref", "want"),
    [("main", _A), ("refs/heads/main", _A), ("v1.0", _B), ("refs/tags/v1.0", _B)],
)
def test_exact_branch_and_tag_refs_resolve(tmp_path, ref, want):
    rows = [
        (_A, "refs/heads/main"),
        (_B, "refs/tags/v1.0"),
        (_C, "refs/pull/1/head"),
        (_D, "refs/heads/feature/main"),
    ]
    assert _resolve(tmp_path, ref, rows) == want


def test_a_short_name_that_is_both_a_branch_and_a_tag_is_ambiguous(tmp_path):
    with pytest.raises(RuntimeError, match="ambiguous") as excinfo:
        _resolve(tmp_path, "v1", [(_A, "refs/heads/v1"), (_B, "refs/tags/v1")])
    assert "refs/heads/v1" in str(excinfo.value)
    assert "refs/tags/v1" in str(excinfo.value)


def test_an_annotated_tag_resolves_to_its_commit_and_is_not_ambiguous(tmp_path):
    assert _resolve(tmp_path, "v1", [(_A, "refs/tags/v1"), (_B, "refs/tags/v1^{}")]) == _B


@pytest.mark.parametrize(
    "ref",
    [
        "refs/pull/1/head",
        "refs/pull/1/merge",
        "refs/merge-requests/1/head",
        "refs/changes/01/1/1",
        "refs/remotes/origin/main",
    ],
)
def test_review_namespace_refs_are_refused_before_any_request(tmp_path, ref):
    with patch("apm_cli.utils.git_env.git_remote_refs") as remote:
        with pytest.raises(RuntimeError, match="branch or tag"):
            GitCache(tmp_path, refresh=False)._ls_remote_resolve(_URL, ref)
    remote.assert_not_called()
