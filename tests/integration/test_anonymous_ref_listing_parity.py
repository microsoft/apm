"""Live parity: in-process anonymous ref listing equals APM's ``git ls-remote`` byte for byte."""

from __future__ import annotations

import os

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.deps.anonymous_ref_listing import _FALLBACK_ENV, list_refs_anonymously
from apm_cli.utils.git_env import git_remote_refs

pytestmark = [
    pytest.mark.requires_network_integration,
    pytest.mark.component,
    pytest.mark.promise("P-OWN-ls-remote-tag-commits"),
]

# Public repositories advertising a symref HEAD, refs/pull/*, heads and
# annotated (peeled) tags.
_REPOS = ("https://github.com/octocat/Hello-World", "https://github.com/actions/checkout")


@pytest.mark.parametrize("url", _REPOS)
@pytest.mark.parametrize("include_heads", [True, False], ids=["tags-heads", "tags"])
def test_listing_matches_git_ls_remote(url, include_heads):
    # The env APM builds for the anonymous github.com attempt, from a caller env
    # without fallback triggers, so a None here is a real miss. The oracle is
    # the git path that attempt runs today (git_remote_refs, same env).
    blocked = {*_FALLBACK_ENV, "HTTP_PROXY", "http_proxy"}
    caller = {
        k: v
        for k, v in os.environ.items()
        if k == "GIT_EXEC_PATH" or (not k.startswith(("GIT_", "GH_")) and k not in blocked)
    }
    env = AuthResolver.build_public_github_anonymous_git_env(base_env=caller)
    options = ("--tags", "--heads") if include_heads else ("--tags",)

    listed = list_refs_anonymously(url, include_heads=include_heads, env=env)
    git = git_remote_refs(url, env=dict(env), options=options)

    assert git.returncode == 0, git.stderr
    assert listed is not None
    assert listed == git.stdout
