"""The authenticated in-process ref listing equals git's (W3d LA1-LA5 on A1-A10).

Lever 1: inside the attempt that holds a token, ``list_refs_authenticated``
answers ``git ls-remote --tags --heads`` in process with only AuthResolver's
github-basic header, per request. Anything else (no managed header, a user
extraHeader, a challenge, a redirect) answers ``None`` and git runs as before.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.deps.anonymous_ref_listing import list_refs_authenticated
from tests.fixtures.perf import TREE_RECIPES, build_tree, serve_offline
from tests.integration.test_inprocess_fetch_world import _REVOKED, _TOKEN, _basic, _private

pytestmark = [
    pytest.mark.integration,
    pytest.mark.component,
    pytest.mark.windows_compat,
    pytest.mark.promise("P-OWN-git-transport-selection", "P-OWN-host-credential-resolution"),
]


def _listing(request) -> bool:
    """An info/refs request of the in-process client (git's own carry a ``git/`` agent)."""
    agent = request.header("User-Agent") or ""
    return "/info/refs" in request.path and not agent.startswith("git/")


def _git_listing(request) -> bool:
    agent = request.header("User-Agent") or ""
    return "/info/refs" in request.path and agent.startswith("git/")


def _world(tmp_path: Path):
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    url = f"https://github.com/{tree.slug(sorted(tree.packages)[0])}"
    return tree, url


def _attempt_env(world, token: str = _TOKEN) -> dict[str, str]:
    """The env AuthResolver builds for a github-basic token attempt (the managed header)."""
    return AuthResolver.build_public_github_authenticated_git_env(token, base_env=dict(world.env))


def _git_ls_remote(url: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    from apm_cli.utils.git_env import git_remote_refs

    return git_remote_refs(url, env=env, options=("--tags", "--heads"))


def test_a_private_listing_in_process_equals_git_with_the_same_header(tmp_path: Path) -> None:
    """LA1/LA5: byte-equal to git ls-remote; the only header sent is git's own extraheader."""
    tree, url = _world(tmp_path)
    with serve_offline(tree, tmp_path / "world") as world:
        _private(world)
        env = _attempt_env(world)
        world.services.proxy.clear()

        listed = list_refs_authenticated(url, include_heads=True, env=env)
        git = _git_ls_remote(url, env)

        in_process = [r for r in world.services.proxy.observations if _listing(r)]
        by_git = [r for r in world.services.proxy.observations if _git_listing(r)]

    assert git.returncode == 0, git.stderr
    assert listed is not None
    assert listed == git.stdout
    assert len(in_process) == 1
    assert {r.header("Authorization") for r in in_process} == {_basic(_TOKEN)}
    assert {r.header("Authorization") for r in by_git} == {_basic(_TOKEN)}


def test_a_redirect_falls_back_and_never_carries_the_header_away(tmp_path: Path) -> None:
    """LA3/A2: a 302 to another host answers None; nothing reaches the other host."""
    tree, url = _world(tmp_path)
    with serve_offline(tree, tmp_path / "world") as world:
        world.services.script(
            "github.com",
            "/",
            302,
            {"Location": "https://elsewhere.invalid/acme/pkg/info/refs"},
            b"",
            when=_listing,
        )
        world.services.proxy.clear()

        listed = list_refs_authenticated(url, include_heads=True, env=_attempt_env(world))
        hosts = {r.host for r in world.services.proxy.observations}

    assert listed is None
    assert hosts == {"github.com"}


def test_a_revoked_token_falls_back_to_git_which_fails_as_today(tmp_path: Path) -> None:
    """LA3/A5: a challenge answers None, so git runs that same attempt and fails as today."""
    tree, url = _world(tmp_path)
    with serve_offline(tree, tmp_path / "world") as world:
        _private(world)
        env = _attempt_env(world, _REVOKED)

        listed = list_refs_authenticated(url, include_heads=True, env=env)
        git = _git_ls_remote(url, env)

    assert listed is None
    assert git.returncode != 0


def test_a_user_extra_header_sends_the_listing_to_git(tmp_path: Path) -> None:
    """A6: a user http.extraHeader beside APM's token means git; no in-process request."""
    tree, url = _world(tmp_path)
    with serve_offline(tree, tmp_path / "world") as world:
        with open(world.env["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8", newline="\n") as config:
            config.write('[http "https://github.com/"]\n\textraHeader = X-Corp-Proxy: 1\n')
        world.services.proxy.clear()

        listed = list_refs_authenticated(url, include_heads=True, env=_attempt_env(world))
        in_process = [r for r in world.services.proxy.observations if _listing(r)]

    assert listed is None
    assert in_process == []


def test_an_env_without_apms_header_never_lists_in_process(tmp_path: Path) -> None:
    """LA1: no managed github-basic header (anonymous env) answers None for this listing."""
    tree, url = _world(tmp_path)
    with serve_offline(tree, tmp_path / "world") as world:
        world.services.proxy.clear()

        listed = list_refs_authenticated(url, include_heads=True, env=dict(world.env))
        in_process = [r for r in world.services.proxy.observations if _listing(r)]

    assert listed is None
    assert in_process == []


def test_the_token_never_reaches_debug_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """LA4/A4: neither a hit nor a fallback logs the token or its header."""
    tree, url = _world(tmp_path)
    with serve_offline(tree, tmp_path / "world") as world, caplog.at_level(logging.DEBUG):
        hit = list_refs_authenticated(url, include_heads=True, env=_attempt_env(world))
        _private(world)
        miss = list_refs_authenticated(url, include_heads=True, env=_attempt_env(world, _REVOKED))

    assert hit is not None and miss is None
    for token in (_TOKEN, _REVOKED):
        assert token not in caplog.text
        assert _basic(token).split(" ", 1)[1] not in caplog.text


def test_a_rate_limited_listing_stops_every_later_in_process_request(tmp_path: Path) -> None:
    """LA3 (W3d R1): the first listing 429 trips the command's breaker; git lists the rest."""
    from apm_cli.core.invocation_memo import invocation_scope
    from apm_cli.deps.anonymous_ref_listing import list_refs_anonymously

    tree, url = _world(tmp_path)
    other = f"https://github.com/{tree.slug(sorted(tree.packages)[1])}"
    with serve_offline(tree, tmp_path / "world") as world:
        throttled: list[object] = []

        def first_listing(request) -> bool:
            if throttled or not _listing(request):
                return False
            throttled.append(request)
            return True

        world.services.script(
            "github.com", "/", 429, {"Retry-After": "60"}, b"", when=first_listing
        )
        world.services.proxy.clear()
        anonymous = AuthResolver.build_public_github_anonymous_git_env(base_env=dict(world.env))
        with invocation_scope():
            first = list_refs_authenticated(url, include_heads=True, env=_attempt_env(world))
            later = list_refs_authenticated(other, include_heads=True, env=_attempt_env(world))
            unauthenticated = list_refs_anonymously(other, include_heads=True, env=anonymous)
        in_process = [r for r in world.services.proxy.observations if _listing(r)]

    assert (first, later, unauthenticated) == (None, None, None)
    assert [r.status for r in in_process] == [429], "no in-process request after the 429"
