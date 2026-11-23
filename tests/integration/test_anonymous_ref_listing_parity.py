"""Live parity: in-process anonymous ref listing equals ``git ls-remote`` byte for byte."""

from __future__ import annotations

import os
import subprocess

import pytest

from apm_cli.deps.anonymous_ref_listing import list_refs_anonymously

pytestmark = pytest.mark.requires_network_integration

# Public repositories advertising a symref HEAD, refs/pull/*, heads and
# annotated (peeled) tags.
_REPOS = ("https://github.com/octocat/Hello-World", "https://github.com/actions/checkout")


def _git_ls_remote(url: str, *options: str, env: dict[str, str]) -> str:
    return subprocess.run(
        ["git", "ls-remote", *options, url],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=120,
    ).stdout


@pytest.mark.parametrize("url", _REPOS)
@pytest.mark.parametrize("include_heads", [True, False], ids=["tags-heads", "tags"])
def test_listing_matches_git_ls_remote(url, include_heads):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GH_"))}
    env["GIT_TERMINAL_PROMPT"] = "0"
    options = ("--tags", "--heads") if include_heads else ("--tags",)

    listed = list_refs_anonymously(url, include_heads=include_heads, env=env)
    expected = _git_ls_remote(url, *options, env=env)

    assert listed == expected
