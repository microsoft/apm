"""Repository names that end in characters of ``.git`` parse and round-trip (#3715).

``str.rstrip(".git")`` strips a character *set*, so names such as ``git``,
``tig`` or ``gitit`` were emptied and rejected. The canonical validator must
remove only the literal ``.git`` suffix, reject segments made only of dots or
names that still end in ``.git`` (they would be written back as a different
repository), and every accepted reference must survive ``apm.yml``
serialization.
"""

from __future__ import annotations

import string

import pytest
from hypothesis import example, given, seed, settings
from hypothesis import strategies as st

from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.unit

PROPERTY_SEED = 0x3715
PROPERTY_PROFILE = settings(max_examples=60, deadline=None, database=None, print_blob=True)


@pytest.mark.parametrize(
    ("spec", "repo_url"),
    [
        ("git/git", "git/git"),
        ("jonas/tig", "jonas/tig"),
        ("owner/gitit", "owner/gitit"),
        ("owner/git.git", "owner/git"),
        ("gitlab.com/git/git", "git/git"),
        ("dev.azure.com/org/proj/git", "org/proj/git"),
        ("https://github.com/git/git", "git/git"),
        ("git@github.com:jonas/tig.git", "jonas/tig"),
    ],
)
def test_names_ending_in_git_characters_parse_and_round_trip(spec: str, repo_url: str) -> None:
    first = DependencyReference.parse(spec)
    assert first.repo_url == repo_url
    second = DependencyReference.parse(first.to_apm_yml_entry())
    assert (second.host, second.repo_url) == (first.host, repo_url)


@pytest.mark.parametrize(
    "spec",
    [
        "owner/.git",
        "owner/...",
        "owner/....git",
        "https://github.com/owner/...",
        "https://github.com/owner/%2E%2E%2E",
        "dev.azure.com/org/proj/...",
        "owner/git.git.git",
        "owner/.git.git",
        "https://github.com/owner/git.git.git",
        "git@github.com:owner/git.git.git",
        "gitlab.com/group/tig.git.git",
        "dev.azure.com/org/proj/git.git.git",
    ],
)
def test_unrepresentable_repository_segments_are_rejected(spec: str) -> None:
    with pytest.raises(ValueError):
        DependencyReference.parse(spec)


_OWNER = st.text(string.ascii_letters + string.digits + "-", min_size=1, max_size=12).filter(
    lambda s: not s.startswith("-") and not s.endswith("-")
)
_REPO = st.text(string.ascii_letters + string.digits + "._-", min_size=1, max_size=16).filter(
    lambda s: s.removesuffix(".git").strip(".") != "" and not s.removesuffix(".git").endswith(".git")
)


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(owner=_OWNER, repo=_REPO)
@example(owner="git", repo="git")
@example(owner="jonas", repo="tig")
@example(owner="owner", repo="gitit")
def test_https_form_round_trips_through_manifest_entry(owner: str, repo: str) -> None:
    first = DependencyReference.parse(f"https://github.com/{owner}/{repo}")
    entry = first.to_apm_yml_entry()
    assert isinstance(entry, str)
    second = DependencyReference.parse(entry)
    assert second.repo_url == first.repo_url
    assert second.host == first.host
    assert second.to_apm_yml_entry() == entry
