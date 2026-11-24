"""The effective Git config probe runs once per invocation and never serves stale config.

Each cell counts real ``git config`` runs in a ``GIT_TRACE`` file and compares
the memoized read with a fresh probe (memo off) after a config change inside
one invocation. The killed control shows the content hash is what catches a
same-size edit.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.core.invocation_memo import invocation_scope
from apm_cli.utils import git_config_memo, git_env
from apm_cli.utils.git_env import GitUrlRewriteProbeError

pytestmark = [
    pytest.mark.component,
    pytest.mark.windows_compat,
    pytest.mark.promise("P-OWN-git-child-repository-isolation"),
]


class _World:
    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.home.mkdir()
        self.trace = root / "git-trace.log"
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_") and key not in {"HOME", "XDG_CONFIG_HOME", "USERPROFILE"}
        }
        self.env.update(
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            XDG_CONFIG_HOME=str(root / "xdg"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TRACE=str(self.trace),
        )

    def probes(self) -> int:
        """Real ``git config`` children so far, read from Git's own trace."""
        if not self.trace.exists():
            return 0
        return self.trace.read_text(encoding="utf-8", errors="replace").count(
            "built-in: git config "
        )

    def read(self, **kwargs):
        return git_env._read_effective_git_config(self.env, **kwargs)

    def fresh(self, **kwargs):
        """The memo-off answer: a direct probe, with no invocation memo."""
        return git_env._probe_effective_git_config(self.env, **kwargs)


def _assert_probes(world: _World, expected: int) -> None:
    """Assert the number of real ``git config`` children Git traced."""
    trace = ""
    if world.trace.exists():
        trace = world.trace.read_text(encoding="utf-8", errors="replace")
    assert trace.count("built-in: git config ") == expected, trace


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


def test_a_repeated_probe_runs_once_inside_an_invocation_and_every_time_outside(
    world: _World,
) -> None:
    (world.home / ".gitconfig").write_text('[url "https://a.example/"]\n\tinsteadOf = gh:\n')

    world.read()
    world.read()
    _assert_probes(world, 2)  # no invocation, no memo

    with invocation_scope():
        first = world.read()
        second = world.read()
    _assert_probes(world, 3)
    assert second is first and first.rewrites == (("https://a.example/", "gh:"),)


def _edit_global_same_size(world: _World) -> None:
    path = world.home / ".gitconfig"
    path.write_text(path.read_text().replace("https://a.example/", "https://b.example/"))


def _add_command_config(world: _World) -> None:
    world.env.update(
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="url.https://c.example/.insteadOf",
        GIT_CONFIG_VALUE_0="cc:",
    )


def _switch_home(world: _World) -> None:
    other = world.home.parent / "other-home"
    other.mkdir()
    (other / ".gitconfig").write_text('[url "https://d.example/"]\n\tinsteadOf = dd:\n')
    world.env.update(HOME=str(other), USERPROFILE=str(other))


def _rewrite_in_include(world: _World) -> None:
    (world.home / "inc.cfg").write_text('[url "https://e.example/"]\n\tinsteadOf = ee:\n')


def _create_xdg_global(world: _World) -> None:
    xdg = Path(world.env["XDG_CONFIG_HOME"]) / "git"
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
def test_a_config_change_mid_invocation_is_seen_like_a_fresh_probe(world: _World, change) -> None:
    (world.home / ".gitconfig").write_text(
        '[url "https://a.example/"]\n\tinsteadOf = gh:\n[include]\n\tpath = inc.cfg\n'
    )
    with invocation_scope():
        before = world.read()
        world.read()  # the include target is fingerprinted and kept from here
        kept_probes = world.probes()
        world.read()
        _assert_probes(world, kept_probes)  # served from the memo
        change(world)
        after = world.read()
        _assert_probes(world, kept_probes + 1)  # the change forced a real probe
    fresh = world.fresh()
    assert after.rewrites == fresh.rewrites
    assert after.rewrites != before.rewrites


def test_an_onbranch_include_is_never_kept(world: _World) -> None:
    (world.home / ".gitconfig").write_text('[includeIf "onbranch:main"]\n\tpath = branch.cfg\n')
    with invocation_scope():
        world.read()
        world.read()
    _assert_probes(world, 2)


def test_a_config_write_by_apm_is_seen_by_the_next_probe(world: _World, tmp_path: Path) -> None:
    repo = tmp_path / "repo.git"
    subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True, env=world.env)
    with invocation_scope():
        world.read(git_dir=repo)
        subprocess.run(
            ["git", "--git-dir", str(repo), "config", "url.https://g.example/.insteadOf", "gg:"],
            check=True,
            env=world.env,
        )
        git_config_memo.invalidate(repo)
        probes = world.probes()
        after = world.read(git_dir=repo)
        _assert_probes(world, probes + 1)
    fresh = world.fresh(git_dir=repo)
    assert after.rewrites == fresh.rewrites
    assert ("https://g.example/", "gg:") in after.rewrites


def _memo_read(world: _World, probe, **kwargs):
    return git_config_memo.read(
        probe,
        world.env,
        git=git_env.get_git_executable(),
        git_dir=None,
        worktree=None,
        config_paths=git_config_memo.git_config_paths,
        **kwargs,
    )


def test_killed_control_without_the_content_hash_a_same_size_edit_goes_stale(
    world: _World,
) -> None:
    """Mutation control: a file identity of size alone serves the stale rewrite."""

    def size_only(path: str):
        identity = git_config_memo._file_id(path)
        return None if identity is None else (*identity[:3], "")

    (world.home / ".gitconfig").write_text('[url "https://a.example/"]\n\tinsteadOf = gh:\n')
    with invocation_scope():
        _memo_read(world, world.fresh, file_id=size_only)
        _edit_global_same_size(world)
        stale = _memo_read(world, world.fresh, file_id=size_only)
        hashed = world.read()
    _assert_probes(world, 2)  # the stale read never asked Git again
    assert stale.rewrites == (("https://a.example/", "gh:"),)
    assert hashed.rewrites == (("https://b.example/", "gh:"),)


def test_a_failed_probe_is_never_kept(world: _World) -> None:
    attempts: list[int] = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise GitUrlRewriteProbeError("Git config probe failed")
        return world.fresh()

    with invocation_scope():
        with pytest.raises(GitUrlRewriteProbeError):
            _memo_read(world, flaky)
        _memo_read(world, flaky)
    assert attempts == [1, 1]
    _assert_probes(world, 1)


def test_a_relative_include_in_a_subdirectory_is_resolved_and_kept(world: _World) -> None:
    (world.home / "conf.d").mkdir()
    extra = world.home / "conf.d" / "extra.cfg"
    extra.write_text('[url "https://h.example/"]\n\tinsteadOf = hh:\n')
    (world.home / ".gitconfig").write_text("[include]\n\tpath = conf.d/extra.cfg\n")
    with invocation_scope():
        world.read()
        kept = world.read()  # kept from the second miss, once the include is known
        world.read()
        _assert_probes(world, 2)
        extra.write_text('[url "https://i.example/"]\n\tinsteadOf = ii:\n')
        after = world.read()
    _assert_probes(world, 3)
    assert kept.rewrites == (("https://h.example/", "hh:"),)
    assert after.rewrites == (("https://i.example/", "ii:"),)


def test_a_commondir_created_after_a_kept_probe_forces_a_real_probe(
    world: _World, tmp_path: Path
) -> None:
    repo = tmp_path / "repo.git"
    subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True, env=world.env)
    with invocation_scope():
        world.read(git_dir=repo)
        world.read(git_dir=repo)
        _assert_probes(world, 1)
        (repo / "commondir").write_text(str(tmp_path / "elsewhere.git") + "\n")
        world.read(git_dir=repo)
        world.read(git_dir=repo)  # a shared common dir is never kept
    _assert_probes(world, 3)


@pytest.mark.skipif(os.name != "nt", reason="drive-less rooted paths exist only on Windows")
def test_a_drive_less_include_on_windows_is_never_kept(world: _World) -> None:
    (world.home / ".gitconfig").write_text("[include]\n\tpath = /mingw64/etc/extra.cfg\n")
    with invocation_scope():
        world.read()
        world.read()
    _assert_probes(world, 2)


def test_concurrent_misses_on_one_key_probe_once(world: _World) -> None:
    import threading

    (world.home / ".gitconfig").write_text('[url "https://a.example/"]\n\tinsteadOf = gh:\n')
    start = threading.Barrier(4)
    results: list[object] = []

    def worker() -> None:
        start.wait()
        results.append(world.read())

    with invocation_scope():
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    _assert_probes(world, 1)
    assert len({id(result) for result in results}) == 1


@pytest.mark.skipif(os.name == "nt", reason="Git expands ~user only on POSIX")
def test_a_named_user_include_is_never_kept(world: _World) -> None:
    import getpass

    (world.home / ".gitconfig").write_text(f"[include]\n\tpath = ~{getpass.getuser()}/no.cfg\n")
    with invocation_scope():
        world.read()
        world.read()
    _assert_probes(world, 2)
