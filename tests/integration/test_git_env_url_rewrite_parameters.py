"""The URL-only probe env keeps only url rewrites from GIT_CONFIG_PARAMETERS (#5246).

PROMISE: P-OWN-git-child-repository-isolation -- the env that decides which
server answers for a URL keeps the ``url.<base>.insteadOf`` and
``pushInsteadOf`` entries of ``GIT_CONFIG_PARAMETERS`` and drops every
other entry; userinfo never survives in a kept entry, and an entry that
cannot be parsed with certainty is dropped (source: #5246 scope record).

Each cell asks git itself which command-scope config the probe env carries.
"""

from __future__ import annotations

import subprocess

import pytest

from apm_cli.cache.commit_reachability import _url_only_env

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-git-child-repository-isolation")]

_MIRROR = "https://mirror.example/"
_ORIGIN = "https://github.com/"


def _git_sees(params: str, tmp_path) -> subprocess.CompletedProcess:
    """``git config --list --show-scope`` under the probe env built from *params*."""
    env = _url_only_env({"GIT_CONFIG_PARAMETERS": params})
    child = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path), "PATH": env.get("PATH", "")}
    if "GIT_CONFIG_PARAMETERS" in env:
        child["GIT_CONFIG_PARAMETERS"] = env["GIT_CONFIG_PARAMETERS"]
    return subprocess.run(
        ["git", "config", "--list", "--show-scope"],
        cwd=tmp_path,
        env=child,
        capture_output=True,
        text=True,
        check=False,
    )


def test_an_insteadof_and_a_pushinsteadof_are_kept(tmp_path) -> None:
    params = f"'url.{_MIRROR}.insteadOf'='{_ORIGIN}' 'url.{_MIRROR}.pushInsteadOf'='{_ORIGIN}'"

    seen = _git_sees(params, tmp_path)

    assert seen.returncode == 0, seen.stderr
    assert seen.stdout.splitlines() == [
        f"command\turl.{_MIRROR}.insteadof={_ORIGIN}",
        f"command\turl.{_MIRROR}.pushinsteadof={_ORIGIN}",
    ]


def test_an_extraheader_and_a_credential_helper_are_dropped(tmp_path) -> None:
    params = (
        "'http.extraHeader'='Authorization: Bearer sentinel-token' "
        "'credential.helper'='store' "
        f"'url.{_MIRROR}.insteadOf'='{_ORIGIN}'"
    )

    seen = _git_sees(params, tmp_path)

    assert seen.returncode == 0, seen.stderr
    assert seen.stdout.splitlines() == [f"command\turl.{_MIRROR}.insteadof={_ORIGIN}"]
    assert "sentinel-token" not in seen.stdout + seen.stderr


def test_userinfo_in_a_kept_rewrite_never_survives(tmp_path) -> None:
    params = f"'url.https://user:sentinel-token@mirror.example/.insteadOf'='{_ORIGIN}'"

    seen = _git_sees(params, tmp_path)

    assert seen.returncode == 0, seen.stderr
    assert "sentinel-token" not in seen.stdout + seen.stderr
    assert "command\t" not in seen.stdout


@pytest.mark.parametrize(
    "params",
    [
        f"'url.{_MIRROR}.insteadOf'='{_ORIGIN}",  # unterminated quote
        f"url.{_MIRROR}.insteadOf={_ORIGIN}",  # unquoted
    ],
)
def test_an_entry_that_cannot_be_parsed_is_dropped(params: str, tmp_path) -> None:
    seen = _git_sees(params, tmp_path)

    assert seen.returncode == 0, seen.stderr
    assert "command\t" not in seen.stdout
