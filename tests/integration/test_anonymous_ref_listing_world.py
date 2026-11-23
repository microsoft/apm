"""In-process anonymous ref listing under a realistic user gitconfig, in the offline world.

A developer's global config carries credential helpers (Git Credential
Manager, gh), credential.interactive, URL-scoped useHttpPath and SSH
rewrites for other hosts. Under the env APM builds for the anonymous
github.com attempt, none of these change git's request, so the fast path
must answer for every package, byte-identical to git. Without APM's fence a
configured helper survives and the listing must fall back to git.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.deps.anonymous_ref_listing import list_refs_anonymously
from apm_cli.utils.git_env import git_remote_refs
from tests.fixtures.perf import TREE_RECIPES, build_tree, serve_offline

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-git-transport-selection", "P-OWN-ls-remote-tag-commits"),
]

USER_GITCONFIG = """\
[user]
\tname = Dev User
\temail = dev@example.invalid
[init]
\tdefaultBranch = main
[pull]
\trebase = true
[credential]
\thelper = manager
\tinteractive = never
[credential "https://github.com"]
\thelper =
\thelper = !gh auth git-credential
[credential "https://dev.azure.com"]
\tuseHttpPath = true
[url "git@gitlab.example.invalid:"]
\tinsteadOf = https://gitlab.example.invalid/
"""


@pytest.fixture
def user_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fixture = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(fixture, tmp_path / "world") as world:
        with open(world.env["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8", newline="\n") as config:
            config.write(USER_GITCONFIG)
        # APM reads the parent config from its own process environment.
        for name, value in world.env.items():
            monkeypatch.setenv(name, value)
        yield fixture, dict(world.env)


def _urls(fixture) -> list[str]:
    return [f"https://github.com/{fixture.slug(name)}" for name in sorted(fixture.packages)]


def test_fast_path_answers_every_package_under_a_realistic_user_config(user_world, caplog):
    fixture, world_env = user_world
    env = AuthResolver.build_public_github_anonymous_git_env(base_env=world_env)
    hits = 0
    for url in _urls(fixture):
        with caplog.at_level(logging.DEBUG, logger="apm_cli.deps.anonymous_ref_listing"):
            listed = list_refs_anonymously(url, include_heads=True, env=env)
        git = git_remote_refs(url, env=dict(env), options=("--tags", "--heads"))

        assert git.returncode == 0, git.stderr
        assert listed == git.stdout, caplog.text
        hits += listed is not None

    assert hits == len(fixture.packages) == 10


def test_a_configured_helper_still_falls_back_to_git(user_world):
    fixture, world_env = user_world
    url = _urls(fixture)[0]

    listed = list_refs_anonymously(url, include_heads=False, env=world_env)
    git = git_remote_refs(url, env=dict(world_env), options=("--tags",))

    assert listed is None
    assert git.returncode == 0, git.stderr
    assert "refs/tags/" in git.stdout
