"""Config-once answers exactly what Git answers, or asks Git (rank 5, W3d cells).

``git_config_once.inprocess_header_group`` replaces ``git config --get-urlmatch
http.extraHeader`` only for plain http(s) URLs and keys; every shape it
answers is compared here with the real Git, and every shape it must not
answer (userinfo, wildcards, percent-encoding, dot segments, two spellings of
one URL) returns ``None`` so the caller spawns Git. ``git_config_once.clone_env``
skips the throwaway probe repository only when no include can select config
by repository, re-reads config that changed mid-command, and yields the same
clone env as the probe repository.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.core.invocation_memo import invocation_scope
from apm_cli.utils import git_config_once, git_env
from apm_cli.utils.git_env import GitConfigEntry

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-git-transport-selection"),
]

URL = "https://github.com/owner/repo"


def _headers(*keys: str, empty: tuple[int, ...] = ()) -> tuple[GitConfigEntry, ...]:
    """Header entries; the indexes in *empty* hold an empty value (Git's list reset)."""
    return tuple(
        GitConfigEntry("global", key, "" if index in empty else f"X-Cell: {index}")
        for index, key in enumerate(keys)
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
    pytest.param(
        URL,
        ("http.https://github.com/Owner.extraheader", "http.https://github.com/owner.extraheader"),
        id="case-folded-path-spellings",
    ),
]


def _git_answer(url: str, headers: tuple[GitConfigEntry, ...]) -> tuple[GitConfigEntry, ...]:
    """What ``git config --get-urlmatch`` selects (the config-once shortcuts off)."""
    return git_config_once._spawned(
        git_env._urlmatched_header_group, url, headers, dict(os.environ)
    )


@pytest.mark.parametrize(("url", "keys"), ANSWERED)
def test_in_process_urlmatch_equals_git(url: str, keys: tuple[str, ...]) -> None:
    headers = _headers(*keys)

    fast = git_config_once.inprocess_header_group(url, headers)

    _assert_git_agrees(url, headers, fast)


@pytest.mark.parametrize(("url", "keys"), DEFERRED)
def test_shapes_git_must_decide_are_left_to_git(url: str, keys: tuple[str, ...]) -> None:
    headers = _headers(*keys)

    _assert_git_decides(url, headers)


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
        _assert_probe_repository_used(user_config.parent / "fresh")


def test_a_config_change_mid_command_is_read_again(user_config: Path) -> None:
    with invocation_scope():
        before = git_config_once.clone_env(URL, None, user_config.parent / "fresh", True)
        user_config.write_text(
            '[user]\n\tname = Example\n[http "https://github.com/owner"]\n\textraHeader = X-Cell: 1\n',
            encoding="utf-8",
        )
        after = git_config_once.clone_env(URL, None, user_config.parent / "fresh", True)

    _assert_env_carries(before, "X-Cell: 1", False)
    _assert_env_carries(after, "X-Cell: 1", True)


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
        once = git_config_once.clone_env(URL, None, tmp_path / "clone", True)
        probed = git_config_once._spawned(
            git_env.git_clone_env, URL, None, tmp_path / "clone", bare=True
        )

    _assert_same_env(once, probed)


@pytest.mark.parametrize("scope", ["local", "worktree"])
def test_a_local_entry_keeps_the_probe_repository(scope: str) -> None:
    """W3d D1: local or worktree entries in the repository-less read mean that read ran
    inside some repository (the git executable's own directory), which a fresh probe
    repository does not hold. GIT_DIR is stripped before the read, and the read runs
    outside the working directory, as ``git clone`` does."""
    from apm_cli.utils.git_env import _GitConfigSnapshot

    entries = (
        GitConfigEntry("global", "user.name", "Example"),
        GitConfigEntry(scope, "http.extraheader", "X-Local: 1"),
    )
    snapshot = _GitConfigSnapshot(entries=entries, rewrites=(), http_headers=entries[1:])
    _assert_probe_kept(snapshot)


def _assert_probe_kept(snapshot) -> None:
    """The config-once path declines, so git_clone_env creates the probe repository."""
    assert git_config_once.needs_probe_repository(snapshot) is True


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs a POSIX non-root user",
)
def test_an_unwritable_clone_target_fails_the_clone_and_creates_nothing(
    user_config: Path, tmp_path: Path
) -> None:
    """W3d D3: config-once creates nothing, so the clone itself fails non-zero."""
    import subprocess

    source = tmp_path / "source.git"
    subprocess.run(["git", "init", "-q", "--bare", str(source)], check=True)
    parent = tmp_path / "locked"
    parent.mkdir()
    target = parent / "clone"
    parent.chmod(0o500)
    try:
        with invocation_scope():
            env = git_env.git_clone_env(URL, None, target, bare=True)
        result = subprocess.run(
            ["git", "clone", "-q", "--bare", str(source), str(target)],
            env=env,
            capture_output=True,
            check=False,
        )
    finally:
        parent.chmod(0o700)

    assert result.returncode != 0
    assert not target.exists()


def _seeded_cases(count: int) -> list[tuple[str, tuple[str, ...], tuple[int, ...]]]:
    import random

    rng = random.Random(4939)  # noqa: S311 -- a seeded test corpus, not a secret
    hosts = ["github.com", "GitHub.com", "gitlab.example.com", "ghe.example.com:8443"]
    paths = ["", "/", "/owner", "/owner/", "/own", "/owner/repo", "/Owner", "/owner/repo/x"]
    schemes = ["https", "http"]
    cases = []
    for _ in range(count):
        url = f"{rng.choice(schemes)}://{rng.choice(hosts[:1] + hosts[2:])}/owner/repo"
        keys: list[str] = []
        for _ in range(rng.randint(1, 4)):
            if rng.random() < 0.2:
                keys.append("http.extraheader")
            else:
                keys.append(
                    f"http.{rng.choice(schemes)}://{rng.choice(hosts)}{rng.choice(paths)}.extraheader"
                )
        empty = tuple(index for index in range(len(keys)) if rng.random() < 0.25)
        cases.append((url, tuple(keys), empty))
    return cases


def test_a_seeded_differential_agrees_with_git_on_every_answered_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W3d D7: 240 seeded shapes; every in-process answer equals Git's, and APM_DEBUG
    (the differential) raises on none of them."""
    monkeypatch.setenv("APM_DEBUG", "1")
    answered = 0
    for url, keys, empty in _seeded_cases(240):
        headers = _headers(*keys, empty=empty)
        fast = git_config_once.inprocess_header_group(url, headers)
        if fast is None:
            continue
        answered += 1
        _assert_git_agrees(url, headers, fast)
        _assert_git_agrees(url, headers, git_config_once.header_group(url, headers, os.environ))
    _assert_enough_answered(answered)


def _assert_git_agrees(url: str, headers, fast) -> None:
    """The in-process answer is the group Git selects for the same config."""
    assert fast is not None
    assert fast == _git_answer(url, headers), (url, [entry.key for entry in headers])


def _assert_git_decides(url: str, headers) -> None:
    """No in-process answer: the caller spawns ``git config --get-urlmatch``."""
    assert git_config_once.inprocess_header_group(url, headers) is None
    assert git_config_once.header_group(url, headers, {}) is None


def _assert_probe_repository_used(target: Path) -> None:
    """Config-once declines, so ``git_clone_env`` creates the probe repository."""
    assert git_config_once.clone_env(URL, None, target, True) is None


def _assert_env_carries(env, header: str, present: bool) -> None:
    """The clone env a git child gets carries (or not) the configured header."""
    assert env is not None
    assert any(header in value for value in env.values()) is present


def _assert_same_env(once, probed) -> None:
    """A git child gets the same env with or without the probe repository."""
    assert once is not None
    assert once == probed


def _assert_enough_answered(answered: int) -> None:
    assert answered >= 100


GENERIC = "http.extraheader"
HOST_KEY = "http.https://github.com.extraheader"


@pytest.mark.parametrize(
    ("keys", "empty"),
    [
        pytest.param((GENERIC, HOST_KEY), (1,), id="generic-value-url-key-empty"),
        pytest.param((HOST_KEY, HOST_KEY), (1,), id="url-key-value-then-same-key-empty"),
        pytest.param((GENERIC, HOST_KEY), (0,), id="generic-empty-specific-value"),
    ],
)
def test_an_empty_header_value_selects_what_git_selects(
    keys: tuple[str, ...], empty: tuple[int, ...]
) -> None:
    """W3d D6: an empty extraHeader value resets Git's list; the selected group, empty
    entries included, equals Git's."""
    headers = _headers(*keys, empty=empty)
    _assert_git_agrees(URL, headers, git_config_once.inprocess_header_group(URL, headers))


def test_port_zero_is_left_to_git() -> None:
    """Git rejects port 0 in a URL key, so it never overrides the generic header here."""
    _assert_git_decides(URL, _headers(GENERIC, "http.https://github.com:0.extraheader"))
