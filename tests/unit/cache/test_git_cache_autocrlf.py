"""Real-git regression for git-subpath content_hash CRLF invariance (apm#2971).

GitCache is the default materialization path for ``owner/repo/<subdir>#ref``
dependencies. Host ``core.autocrlf=true`` (Git for Windows default) must not
change working-tree bytes or the raw package hash of LF-committed content.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.cache.git_cache import GitCache, _safe_git_args
from apm_cli.utils.content_hash import compute_package_hash

_LF_BODY = b"---\nname: demo\n---\nhello\nworld\n"


def _git(
    args: list[str], *, cwd: Path | None = None, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _neutral_git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env.pop("GIT_CONFIG_COUNT", None)
    for key in list(env):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key, None)
    return env


def _lf_origin(tmp_path: Path) -> tuple[Path, str]:
    """Commit LF skill bytes and return (origin path, sha)."""
    src = tmp_path / "origin"
    skill = src / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(_LF_BODY)
    env = _neutral_git_env()
    _git(["init", "-b", "main", str(src)], env=env)
    _git(["-C", str(src), "config", "user.email", "test@example.com"], env=env)
    _git(["-C", str(src), "config", "user.name", "APM Test"], env=env)
    _git(["-C", str(src), "config", "core.autocrlf", "false"], env=env)
    _git(["-C", str(src), "add", "."], env=env)
    _git(["-C", str(src), "commit", "-q", "-m", "lf fixture"], env=env)
    sha = _git(["-C", str(src), "rev-parse", "HEAD"], env=env).stdout.strip()
    return src, sha


def _host_autocrlf_true_env(tmp_path: Path) -> dict[str, str]:
    system_cfg = tmp_path / "system.gitconfig"
    system_cfg.write_text(
        "[core]\n\tautocrlf = true\n[safe]\n\tbareRepository = all\n",
        encoding="ascii",
    )
    env = _neutral_git_env()
    env["GIT_CONFIG_SYSTEM"] = str(system_cfg)
    return env


@pytest.mark.windows_compat
def test_safe_git_args_pin_autocrlf_false() -> None:
    args = _safe_git_args()
    assert "core.autocrlf=false" in args


@pytest.mark.windows_compat
def test_full_checkout_keeps_lf_under_system_autocrlf_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, sha = _lf_origin(tmp_path)
    host_env = _host_autocrlf_true_env(tmp_path)
    for key, value in host_env.items():
        monkeypatch.setenv(key, value)
    for key in list(os.environ):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")) or key == "GIT_CONFIG_COUNT":
            monkeypatch.delenv(key, raising=False)

    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), sha, locked_sha=sha)
    skill = checkout / "skills" / "demo" / "SKILL.md"
    assert skill.read_bytes() == _LF_BODY
    config = (checkout / ".git" / "config").read_text(encoding="utf-8")
    assert "autocrlf = false" in config or "autocrlf=false" in config
    assert compute_package_hash(checkout / "skills" / "demo") == compute_package_hash(
        origin / "skills" / "demo"
    )


@pytest.mark.windows_compat
def test_sparse_checkout_keeps_lf_when_env_freezes_autocrlf_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git_network_env freezes host autocrlf into GIT_CONFIG_KEY_n; only -c outranks it."""
    origin, sha = _lf_origin(tmp_path)
    host_env = _host_autocrlf_true_env(tmp_path)
    for key, value in host_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    for key in list(os.environ):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            monkeypatch.delenv(key, raising=False)

    checkout = GitCache(tmp_path / "cache").get_checkout(
        str(origin),
        sha,
        locked_sha=sha,
        sparse_paths=["skills"],
    )
    skill = checkout / "skills" / "demo" / "SKILL.md"
    assert skill.read_bytes() == _LF_BODY
    assert b"\r\n" not in skill.read_bytes()


@pytest.mark.windows_compat
def test_cache_hit_rematerializes_unpinned_crlf_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, sha = _lf_origin(tmp_path)
    host_env = _host_autocrlf_true_env(tmp_path)
    for key, value in host_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    for key in list(os.environ):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            monkeypatch.delenv(key, raising=False)

    cache = GitCache(tmp_path / "cache")
    poisoned = cache.get_checkout(str(origin), sha, locked_sha=sha)
    skill = poisoned / "skills" / "demo" / "SKILL.md"
    skill.write_bytes(b"---\r\nname: demo\r\n---\r\nhello\r\nworld\r\n")
    git_config = poisoned / ".git" / "config"
    text = git_config.read_text(encoding="utf-8")
    text = text.replace("autocrlf = false", "autocrlf = true").replace(
        "autocrlf=false", "autocrlf=true"
    )
    if "autocrlf" not in text:
        text += "\n[core]\n\tautocrlf = true\n"
    git_config.write_text(text, encoding="utf-8")

    reused = cache.get_checkout(str(origin), sha, locked_sha=sha)
    assert reused.exists()
    assert (reused / "skills" / "demo" / "SKILL.md").read_bytes() == _LF_BODY
