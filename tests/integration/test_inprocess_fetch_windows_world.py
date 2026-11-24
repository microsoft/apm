"""The in-process pinned fetch under a Git for Windows-like config, in the offline world.

Git for Windows' system config (credential helper, git-lfs filters,
autocrlf, sslBackend/sslCAInfo) and its gitattributes file of diff drivers
change neither git's anonymous fetch nor, under APM's -c pins, its checkout
bytes. So the in-process path must engage for every package and land the
same bytes and the same lockfile content hash as the git path
(APM_NO_INPROCESS_FETCH=1), which this compares package by package.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.cache.checkout_verify import inprocess_verified
from apm_cli.cache.git_cache import GitCache
from apm_cli.core.auth import AuthResolver
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.git_env import git_remote_refs
from tests.fixtures.perf import TREE_RECIPES, build_tree, serve_offline

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-git-transport-selection"),
]

GIT_FOR_WINDOWS_LIKE = """\
[diff "astextplain"]
\ttextconv = astextplain
[filter "lfs"]
\tclean = git-lfs clean -- %f
\tsmudge = git-lfs smudge -- %f
\tprocess = git-lfs filter-process
\trequired = true
[core]
\tautocrlf = true
\tfscache = true
\tsymlinks = false
[credential]
\thelper = manager
[credential "https://dev.azure.com"]
\tuseHttpPath = true
"""

ATTRIBUTES = "*.c\tdiff=cpp\n*.doc\tdiff=astextplain\n*.pdf\tdiff=astextplain\n"


@pytest.fixture
def windows_like_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fixture = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(fixture, tmp_path / "world") as world:
        with open(world.env["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8", newline="\n") as config:
            config.write(GIT_FOR_WINDOWS_LIKE)
        attributes = Path(world.env["XDG_CONFIG_HOME"]) / "git" / "attributes"
        attributes.parent.mkdir(parents=True, exist_ok=True)
        attributes.write_text(ATTRIBUTES, encoding="utf-8")
        for name, value in world.env.items():
            monkeypatch.setenv(name, value)
        yield fixture, dict(world.env)


def _pins(fixture, env) -> list[tuple[str, str]]:
    pins = []
    for name in sorted(fixture.packages):
        url = f"https://github.com/{fixture.slug(name)}"
        listed = git_remote_refs(url, env=dict(env), options=("--tags",))
        assert listed.returncode == 0, listed.stderr
        refs = dict(line.split("\t")[::-1] for line in listed.stdout.splitlines() if line)
        sha = (
            refs.get(f"refs/tags/{fixture.recipe.tag}^{{}}")
            or refs[f"refs/tags/{fixture.recipe.tag}"]
        )
        pins.append((url, sha))
    return pins


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def test_in_process_engages_and_lands_the_bytes_and_hash_git_lands(windows_like_world, tmp_path):
    fixture, world_env = windows_like_world
    env = AuthResolver.build_public_github_anonymous_git_env(base_env=world_env)
    forced = {**env, "APM_NO_INPROCESS_FETCH": "1"}
    in_process = GitCache(tmp_path / "cache-inprocess")
    via_git = GitCache(tmp_path / "cache-git")

    pins = _pins(fixture, env)
    for url, sha in pins:
        fast = in_process.get_checkout(url, sha, locked_sha=sha, env=env)
        slow = via_git.get_checkout(url, sha, locked_sha=sha, env=forced)

        assert inprocess_verified(fast, sha, use="links"), url
        assert not inprocess_verified(slow, sha, use="links"), url
        assert _tree(fast) == _tree(slow), url
        assert compute_package_hash(fast) == compute_package_hash(slow), url
    assert len(pins) == len(fixture.packages) == 10


def test_the_kept_network_env_equals_a_fresh_one(windows_like_world):
    """G6: with and without the per-command memo, git children get the same env."""
    from apm_cli.cache import inprocess_fetch
    from apm_cli.core.invocation_memo import invocation_scope
    from apm_cli.utils.git_env import git_network_env

    _fixture, world_env = windows_like_world
    env = AuthResolver.build_public_github_anonymous_git_env(base_env=world_env)
    fresh = git_network_env(inprocess_fetch._PROBE_URL, dict(env))

    with invocation_scope():
        first = inprocess_fetch._network_env(env)
        kept = inprocess_fetch._network_env(env)

    assert first == fresh
    assert kept == fresh
