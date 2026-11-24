"""The effective Git config probe runs once per invocation and never serves stale config.

Every cell compares the memoized read with a fresh probe (memo off) after a
config change inside one invocation; the killed control shows the content
hash is what catches a same-size edit.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.core.invocation_memo import invocation_scope
from apm_cli.utils import git_config_memo, git_env
from apm_cli.utils.git_env import GitUrlRewriteProbeError

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in {"HOME", "XDG_CONFIG_HOME", "USERPROFILE"}
    }
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(tmp_path / "xdg"),
        GIT_CONFIG_NOSYSTEM="1",
    )
    probes: list[int] = []
    real_probe = git_env._probe_effective_git_config

    def counted(*args, **kwargs):
        probes.append(1)
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(git_env, "_probe_effective_git_config", counted)
    return home, env, probes, real_probe


def _rewrites(snapshot) -> tuple[tuple[str, str], ...]:
    return snapshot.rewrites


def _fresh(real_probe, env, **kwargs):
    return real_probe(env, **kwargs)


def test_a_repeated_probe_runs_once_inside_an_invocation_and_every_time_outside(world) -> None:
    home, env, probes, _real = world
    (home / ".gitconfig").write_text('[url "https://a.example/"]\n\tinsteadOf = gh:\n')

    git_env._read_effective_git_config(env)
    git_env._read_effective_git_config(env)
    assert len(probes) == 2  # no invocation, no memo

    probes.clear()
    with invocation_scope():
        first = git_env._read_effective_git_config(env)
        second = git_env._read_effective_git_config(env)
    assert len(probes) == 1
    assert second is first and _rewrites(first) == (("https://a.example/", "gh:"),)


def _edit_global_same_size(home: Path, env: dict[str, str]) -> None:
    path = home / ".gitconfig"
    path.write_text(path.read_text().replace("https://a.example/", "https://b.example/"))


def _add_command_config(home: Path, env: dict[str, str]) -> None:
    env.update(
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="url.https://c.example/.insteadOf",
        GIT_CONFIG_VALUE_0="cc:",
    )


def _switch_home(home: Path, env: dict[str, str]) -> None:
    other = home.parent / "other-home"
    other.mkdir()
    (other / ".gitconfig").write_text('[url "https://d.example/"]\n\tinsteadOf = dd:\n')
    env.update(HOME=str(other), USERPROFILE=str(other))


def _rewrite_in_include(home: Path, env: dict[str, str]) -> None:
    (home / "inc.cfg").write_text('[url "https://e.example/"]\n\tinsteadOf = ee:\n')


def _create_xdg_global(home: Path, env: dict[str, str]) -> None:
    xdg = Path(env["XDG_CONFIG_HOME"]) / "git"
    xdg.mkdir(parents=True)
    (xdg / "config").write_text('[url "https://f.example/"]\n\tinsteadOf = ff:\n')


@pytest.mark.parametrize(
    "change",
    [
        _edit_global_same_size,
        _add_command_config,
        _switch_home,
        _rewrite_in_include,
        _create_xdg_global,
    ],
    ids=lambda fn: fn.__name__.lstrip("_"),
)
def test_a_config_change_mid_invocation_is_seen_like_a_fresh_probe(world, change) -> None:
    home, env, _probes, real_probe = world
    (home / ".gitconfig").write_text(
        '[url "https://a.example/"]\n\tinsteadOf = gh:\n[include]\n\tpath = inc.cfg\n'
    )
    with invocation_scope():
        before = git_env._read_effective_git_config(env)
        git_env._read_effective_git_config(env)  # the include is kept from here
        change(home, env)
        after = git_env._read_effective_git_config(env)
    fresh = _rewrites(_fresh(real_probe, env))
    assert _rewrites(after) == fresh
    assert _rewrites(after) != _rewrites(before)


def test_an_onbranch_include_is_never_kept(world) -> None:
    home, env, probes, _real = world
    (home / ".gitconfig").write_text('[includeIf "onbranch:main"]\n\tpath = branch.cfg\n')
    with invocation_scope():
        git_env._read_effective_git_config(env)
        git_env._read_effective_git_config(env)
    assert len(probes) == 2


def test_a_config_write_by_apm_is_seen_by_the_next_probe(world, tmp_path: Path) -> None:
    _home, env, probes, real_probe = world
    repo = tmp_path / "repo.git"
    subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True, env=env)
    with invocation_scope():
        git_env._read_effective_git_config(env, git_dir=repo)
        subprocess.run(
            ["git", "--git-dir", str(repo), "config", "url.https://g.example/.insteadOf", "gg:"],
            check=True,
            env=env,
        )
        git_config_memo.invalidate(repo)
        probes.clear()
        after = git_env._read_effective_git_config(env, git_dir=repo)
    assert len(probes) == 1
    fresh = _rewrites(_fresh(real_probe, env, git_dir=repo))
    assert _rewrites(after) == fresh
    assert ("https://g.example/", "gg:") in _rewrites(after)


def test_killed_control_without_the_content_hash_a_same_size_edit_goes_stale(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation control: keying files by size alone serves the stale rewrite."""
    home, env, _probes, real_probe = world
    real_file_id = git_config_memo._file_id

    def size_only(path: str):
        file_id = real_file_id(path)
        return None if file_id is None else (*file_id[:3], "")

    monkeypatch.setattr(git_config_memo, "_file_id", size_only)
    (home / ".gitconfig").write_text('[url "https://a.example/"]\n\tinsteadOf = gh:\n')
    with invocation_scope():
        git_env._read_effective_git_config(env)
        _edit_global_same_size(home, env)
        stale = git_env._read_effective_git_config(env)
    fresh = _rewrites(_fresh(real_probe, env))
    assert _rewrites(stale) != fresh


def test_a_failed_probe_is_never_kept(world, monkeypatch: pytest.MonkeyPatch) -> None:
    _home, env, _probes, real_probe = world
    calls: list[int] = []

    def failing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise GitUrlRewriteProbeError("Git config probe failed")
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(git_env, "_probe_effective_git_config", failing)
    with invocation_scope():
        with pytest.raises(GitUrlRewriteProbeError):
            git_env._read_effective_git_config(env)
        git_env._read_effective_git_config(env)
    assert len(calls) == 2
