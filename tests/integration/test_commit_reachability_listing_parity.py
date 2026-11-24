"""Pinned-commit verdicts are identical whether the shared listing answers in process or git does.

Each case runs ``require_reachable_commit`` twice against the offline
github.com world (``tree-10-sha``: tag ``v1.0.0`` on the first commit, main
moved one commit past it): once with the pooled in-process listing, once
with it declined so ``git ls-remote`` answers. Verdicts and recorded
verification markers must match.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from apm_cli.cache.commit_reachability import (
    CommitNotReachableError,
    _Unverified,
    open_verdict_stage,
    require_reachable_commit,
)
from apm_cli.cache.url_normalize import cache_shard_key
from apm_cli.core.auth import AuthResolver
from tests.fixtures.perf import TREE_RECIPES, build_tree, serve_offline

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-ls-remote-tag-commits", "P-OWN-git-transport-selection"),
]

_IN_PROCESS = "apm_cli.deps.anonymous_ref_listing.list_refs_anonymously"


@pytest.fixture
def sha_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    recipe = TREE_RECIPES["tree-10-sha"]
    fixture = build_tree(recipe, tmp_path / "fixture")
    with serve_offline(fixture, tmp_path / "world") as world:
        for name, value in world.env.items():
            monkeypatch.setenv(name, value)
        bares = tmp_path / "world" / "repositories" / recipe.name
        yield fixture, dict(world.env), bares


def _rev(bare: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", ref],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _verdict(root: Path, url: str, sha: str, env: dict[str, str]) -> str:
    try:
        require_reachable_commit(
            root, url, cache_shard_key(url), sha, env=env, origin=url, persist=True
        )
    except _Unverified:
        return "unverified"
    except CommitNotReachableError:
        return "refused"
    return "admitted"


def _run(tmp_path: Path, mode: str, cases, env, monkeypatch) -> tuple[dict, dict, dict]:
    if mode == "git":
        monkeypatch.setattr(_IN_PROCESS, lambda *_a, **_k: None)
    verdicts, markers = {}, {}
    stage = open_verdict_stage()  # one install attempt
    for name, (url, sha) in cases.items():
        root = tmp_path / mode / name
        verdicts[name] = _verdict(root, url, sha, env)
    listings = dict(stage.listings)
    stage.commit()
    for name, (url, sha) in cases.items():
        marker = tmp_path / mode / name / "reachability" / cache_shard_key(url) / "verified" / sha
        markers[name] = marker.exists()
    return verdicts, markers, listings


def test_pin_verdicts_match_git_for_every_pin_shape(sha_world, tmp_path, monkeypatch):
    fixture, world_env, bares = sha_world
    env = AuthResolver.build_public_github_anonymous_git_env(base_env=world_env)
    first, second = sorted(fixture.packages)[:2]
    bare = bares / f"{fixture.recipe.owner}__{first}.git"
    url = f"https://github.com/{fixture.slug(first)}"
    other = f"https://github.com/{fixture.slug(second)}"
    tip, tag = _rev(bare, "main"), _rev(bare, "v1.0.0")
    cases = {
        "tip": (url, tip),
        "tag-non-tip": (url, tag),
        "unknown-commit": (url, "f" * 40),
        "unreadable": (f"https://github.com/{fixture.recipe.owner}/missing-repo", tip),
        "cross-origin": (other, tip),
    }

    pooled, pooled_markers, pooled_listings = _run(tmp_path, "pooled", cases, env, monkeypatch)
    with monkeypatch.context() as forced:
        git, git_markers, git_listings = _run(tmp_path, "git", cases, env, forced)

    assert tip != tag
    assert pooled == git
    assert pooled_markers == git_markers
    assert pooled["tip"] == pooled["tag-non-tip"] == "admitted"
    assert pooled_markers["tip"] and pooled_markers["tag-non-tip"]
    assert pooled["unknown-commit"] == pooled["cross-origin"] == "refused"
    assert pooled["unreadable"] == "unverified"
    # The pooled run answered both served origins in process (one listing
    # each, reused across cases); the declined run fell back to git
    # ls-remote, whose output is byte-identical.
    in_process = {r.args[-1]: r.stdout for r in pooled_listings.values()}
    assert set(in_process) == {url, other}
    assert all(r.args == ["git", "ls-remote", r.args[-1]] for r in pooled_listings.values())
    assert all("ls-remote" in r.args and len(r.args) > 3 for r in git_listings.values())
    assert sorted(r.stdout for r in git_listings.values()) == sorted(in_process.values())
