"""Config-once answers exactly what Git answers, or asks Git (rank 5, W3d cells).

``_inprocess_header_group`` replaces ``git config --get-urlmatch
http.extraHeader`` only for plain http(s) URLs and keys; every shape it
answers is compared here with the real Git, and every shape it must not
answer (userinfo, wildcards, percent-encoding, dot segments, two spellings of
one URL) returns ``None`` so the caller spawns Git. ``_config_once_clone_env``
skips the throwaway probe repository only when no include can select config
by repository, re-reads config that changed mid-command, and yields the same
clone env as the probe repository.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.core.invocation_memo import invocation_scope
from apm_cli.utils import git_env
from apm_cli.utils.git_env import GitConfigEntry

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-git-transport-selection"),
]

URL = "https://github.com/owner/repo"


def _headers(*keys: str) -> tuple[GitConfigEntry, ...]:
    return tuple(
        GitConfigEntry("global", key, f"X-Cell: {index}") for index, key in enumerate(keys)
    )


ANSWERED = [
    pytest.param(URL, ("http.extraheader",), id="generic"),
    pytest.param(URL, ("http.extraheader", "http.https://github.com.extraheader"), id="host-wins"),
    pytest.param(
        URL,
        ("http.https://github.com/owner.extraheader", "http.https://github.com.extraheader"),
        id="path-prefix-at-segment",
    ),
    pytest.param(
        URL,
        ("http.https://github.com/own.extraheader", "http.extraheader"),
        id="prefix-not-at-segment",
    ),
    pytest.param(URL, ("http.https://github.com/owner/.extraheader",), id="trailing-slash"),
    pytest.param(
        URL,
        (
            "http.https://github.com/owner.extraheader",
            "http.https://github.com/owner/repo.extraheader",
        ),
        id="longest-prefix",
    ),
    pytest.param(URL, ("http.https://github.com:443.extraheader",), id="explicit-default-port"),
    pytest.param(
        "https://github.com:8443/owner/repo",
        ("http.https://github.com.extraheader",),
        id="other-port",
    ),
    pytest.param(URL, ("http.http://github.com.extraheader",), id="scheme-mismatch"),
    pytest.param(URL, ("http.https://gitlab.com.extraheader",), id="other-host"),
    pytest.param(URL, ("http.https://GitHub.COM.extraheader",), id="host-case"),
    pytest.param(
        URL,
        ("http.https://github.com.extraheader", "http.https://github.com.extraheader"),
        id="repeated-key",
    ),
]

DEFERRED = [
    pytest.param(
        URL,
        ("http.https://github.com/.extraheader", "http.https://github.com.extraheader"),
        id="two-spellings",
    ),
    pytest.param(URL, ("http.https://user@github.com.extraheader",), id="userinfo-in-key"),
    pytest.param(URL, ("http.https://*.github.com.extraheader",), id="wildcard"),
    pytest.param(URL, ("http.https://github.com/own%65r.extraheader",), id="percent-encoding"),
    pytest.param("https://github.com/owner/../repo", ("http.extraheader",), id="dot-segments"),
    pytest.param("https://user@github.com/owner/repo", ("http.extraheader",), id="userinfo-in-url"),
]


@pytest.mark.parametrize(("url", "keys"), ANSWERED)
def test_in_process_urlmatch_equals_git(url: str, keys: tuple[str, ...]) -> None:
    headers = _headers(*keys)

    fast = git_env._inprocess_header_group(url, headers)

    assert fast is not None
    assert fast == git_env._spawned_header_group(url, headers, dict(os.environ))


@pytest.mark.parametrize(("url", "keys"), DEFERRED)
def test_shapes_git_must_decide_are_left_to_git(
    url: str, keys: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _headers(*keys)
    asked: list[str] = []
    monkeypatch.setattr(
        git_env,
        "_spawned_header_group",
        lambda remote, group, env: asked.append(remote) or (),
    )

    assert git_env._inprocess_header_group(url, headers) is None
    git_env._urlmatched_header_group(url, headers, {})
    assert asked == [url]


@pytest.fixture
def user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private global config the parent and child env both read."""
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = Example\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for name in ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(name, raising=False)
    return config


@pytest.mark.parametrize(
    "include",
    [
        "[include]\n\tpath = {other}\n",
        '[includeIf "gitdir:{root}/"]\n\tpath = {other}\n',
        '[includeIf "hasconfig:remote.*.url:https://github.com/**"]\n\tpath = {other}\n',
    ],
    ids=["include", "includeIf-gitdir", "includeIf-hasconfig"],
)
def test_an_include_keeps_the_probe_repository(user_config: Path, include: str) -> None:
    other = user_config.parent / "other.gitconfig"
    other.write_text("[http]\n\textraHeader = X-Other: 1\n", encoding="utf-8")
    user_config.write_text(
        include.format(other=other.as_posix(), root=user_config.parent.as_posix()),
        encoding="utf-8",
    )

    with invocation_scope():
        assert git_env._config_once_clone_env(URL, None) is None


def test_a_config_change_mid_command_is_read_again(user_config: Path) -> None:
    with invocation_scope():
        before = git_env._config_once_clone_env(URL, None)
        user_config.write_text(
            '[user]\n\tname = Example\n[http "https://github.com/owner"]\n\textraHeader = X-Cell: 1\n',
            encoding="utf-8",
        )
        after = git_env._config_once_clone_env(URL, None)

    assert before is not None and after is not None
    assert before != after
    assert not any("X-Cell: 1" in value for value in before.values())
    assert any("X-Cell: 1" in value for value in after.values())


def test_config_once_env_equals_the_probe_repository_env(user_config: Path, tmp_path: Path) -> None:
    user_config.write_text(
        "[user]\n\tname = Example\n"
        "[credential]\n\thelper = manager\n"
        '[credential "https://github.com"]\n\thelper =\n\thelper = !gh auth git-credential\n'
        '[url "git@gitlab.example.invalid:"]\n\tinsteadOf = https://gitlab.example.invalid/\n'
        '[http "https://gitlab.example.invalid"]\n\textraHeader = X-Other-Host: 1\n',
        encoding="utf-8",
    )

    with invocation_scope():
        once = git_env._config_once_clone_env(URL, None)
        probed = git_env._probed_clone_env(URL, None, tmp_path / "clone", bare=True)

    assert once is not None
    assert once == probed


def test_case_variant_path_keys_are_left_to_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """W3d D4: /Acme and /acme fold to one key but Git compares paths case-sensitively."""
    headers = _headers(
        "http.https://github.com/Acme.extraheader", "http.https://github.com/acme.extraheader"
    )
    url = "https://github.com/acme/repo"
    asked: list[str] = []
    monkeypatch.setattr(
        git_env, "_spawned_header_group", lambda remote, group, env: asked.append(remote) or ()
    )

    assert git_env._inprocess_header_group(url, headers) is None
    git_env._urlmatched_header_group(url, headers, {})
    assert asked == [url]


@pytest.mark.parametrize("scope", ["local", "worktree"])
def test_a_local_or_worktree_entry_keeps_the_probe_repository(
    user_config: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    """W3d D1: a repository-scoped entry means the repo-less read is not what Git will see."""
    real = git_env._read_effective_git_config

    def with_local_entry(env, **kwargs):
        snapshot = real(env, **kwargs)
        extra = GitConfigEntry(scope, "http.https://github.com/.extraheader", "X-Local: 1")
        return git_env._GitConfigSnapshot(
            (*snapshot.entries, extra), snapshot.rewrites, snapshot.http_headers
        )

    monkeypatch.setattr(git_env, "_read_effective_git_config", with_local_entry)

    with invocation_scope():
        assert git_env._config_once_clone_env(URL, None) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_an_unwritable_target_fails_and_leaves_nothing(user_config: Path, tmp_path: Path) -> None:
    """W3d D3: the probe path cannot create its throwaway repository; nothing is left behind."""
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        with invocation_scope(), pytest.raises(Exception):  # noqa: B017 - any refusal is fine
            git_env._probed_clone_env(URL, None, parent / "clone", bare=True)
        assert list(parent.iterdir()) == []
    finally:
        parent.chmod(0o700)
