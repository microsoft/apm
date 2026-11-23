"""Live parity: in-process anonymous ref listing equals ``git ls-remote`` byte for byte."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.deps.anonymous_ref_listing import _FALLBACK_ENV, list_refs_anonymously

pytestmark = [
    pytest.mark.requires_network_integration,
    pytest.mark.component,
    pytest.mark.promise("P-OWN-ls-remote-tag-commits"),
]

# Public repositories advertising a symref HEAD, refs/pull/*, heads and
# annotated (peeled) tags.
_REPOS = ("https://github.com/octocat/Hello-World", "https://github.com/actions/checkout")


def _git_ls_remote(url: str, *options: str, env: dict[str, str]):
    return subprocess.run(
        ["git", "ls-remote", *options, url],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120,
    )


@pytest.mark.parametrize("url", _REPOS)
@pytest.mark.parametrize("include_heads", [True, False], ids=["tags-heads", "tags"])
def test_listing_matches_git_ls_remote(url, include_heads, tmp_path: Path):
    # Both paths get the same controlled git config and a proxy env the fast
    # path supports, so a None here is a real miss, not an intended fallback.
    blocked = {*_FALLBACK_ENV, "HTTP_PROXY", "http_proxy"}
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GIT_", "GH_")) and k not in blocked
    }
    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=str(global_config),
    )
    options = ("--tags", "--heads") if include_heads else ("--tags",)

    listed = list_refs_anonymously(url, include_heads=include_heads, env=env)
    git = _git_ls_remote(url, *options, env=env)

    assert git.returncode == 0, git.stderr
    assert listed is not None
    assert listed == git.stdout
