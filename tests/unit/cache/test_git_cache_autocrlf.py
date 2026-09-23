"""Real-git regression for git-subpath content_hash CRLF invariance (apm#2971).

GitCache is the default materialization path for ``owner/repo/<subdir>#ref``
dependencies. Host ``core.autocrlf=true`` (Git for Windows default) must not
change working-tree bytes or the raw package hash of LF-committed content.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.cache.git_cache import GitCache, _checkout_pins_autocrlf_false, _safe_git_args
from apm_cli.cache.url_normalize import cache_shard_key
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.git_env import get_git_executable

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]

_LF_BODY = b"---\nname: demo\n---\nhello\nworld\n"


def _git(
    args: list[str], *, cwd: Path | None = None, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [get_git_executable(), *args],
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
    env.pop("GIT_CONFIG_NOSYSTEM", None)
    env.pop("GIT_CONFIG_PARAMETERS", None)
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


def _apply_hostile_host_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install system autocrlf=true and drop inherited gitconfig overrides."""
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    for key in list(os.environ):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            monkeypatch.delenv(key, raising=False)
    host_env = _host_autocrlf_true_env(tmp_path)
    for key, value in host_env.items():
        monkeypatch.setenv(key, value)
    observed = _git(["config", "--system", "--get", "core.autocrlf"], env=dict(os.environ))
    assert observed.stdout.strip().lower() == "true"


def test_safe_git_args_pin_autocrlf_false() -> None:
    args = _safe_git_args()
    assert "core.autocrlf=false" in args


def test_full_checkout_keeps_lf_under_system_autocrlf_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, sha = _lf_origin(tmp_path)
    _apply_hostile_host_env(tmp_path, monkeypatch)

    checkout = GitCache(tmp_path / "cache").get_checkout(str(origin), sha, locked_sha=sha)
    skill = checkout / "skills" / "demo" / "SKILL.md"
    assert skill.read_bytes() == _LF_BODY
    assert _checkout_pins_autocrlf_false(checkout)
    assert compute_package_hash(checkout / "skills" / "demo") == compute_package_hash(
        origin / "skills" / "demo"
    )


def test_sparse_checkout_keeps_lf_when_env_freezes_autocrlf_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git_network_env freezes host autocrlf into GIT_CONFIG_KEY_n; only -c outranks it."""
    origin, sha = _lf_origin(tmp_path)
    _apply_hostile_host_env(tmp_path, monkeypatch)

    checkout = GitCache(tmp_path / "cache").get_checkout(
        str(origin),
        sha,
        locked_sha=sha,
        sparse_paths=["skills"],
    )
    skill = checkout / "skills" / "demo" / "SKILL.md"
    assert skill.read_bytes() == _LF_BODY
    assert b"\r\n" not in skill.read_bytes()


def _poison_autocrlf_pin(checkout: Path) -> None:
    skill = checkout / "skills" / "demo" / "SKILL.md"
    skill.write_bytes(b"---\r\nname: demo\r\n---\r\nhello\r\nworld\r\n")
    git_config = checkout / ".git" / "config"
    text = git_config.read_text(encoding="utf-8")
    text = text.replace("autocrlf = false", "autocrlf = true").replace(
        "autocrlf=false", "autocrlf=true"
    )
    if "autocrlf" not in text:
        text += "\n[core]\n\tautocrlf = true\n"
    git_config.write_text(text, encoding="utf-8")


def test_cache_hit_rematerializes_unpinned_crlf_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, sha = _lf_origin(tmp_path)
    _apply_hostile_host_env(tmp_path, monkeypatch)

    cache = GitCache(tmp_path / "cache")
    poisoned = cache.get_checkout(str(origin), sha, locked_sha=sha)
    _poison_autocrlf_pin(poisoned)

    reused = cache.get_checkout(str(origin), sha, locked_sha=sha)
    assert reused.exists()
    assert (reused / "skills" / "demo" / "SKILL.md").read_bytes() == _LF_BODY
    assert _checkout_pins_autocrlf_false(reused)


def test_create_checkout_rematerializes_unpinned_final_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, sha = _lf_origin(tmp_path)
    _apply_hostile_host_env(tmp_path, monkeypatch)

    cache = GitCache(tmp_path / "cache")
    poisoned = cache.get_checkout(str(origin), sha, locked_sha=sha)
    _poison_autocrlf_pin(poisoned)
    assert not _checkout_pins_autocrlf_false(poisoned)

    rebuilt = cache._create_checkout(str(origin), cache_shard_key(str(origin)), sha)
    assert (rebuilt / "skills" / "demo" / "SKILL.md").read_bytes() == _LF_BODY
    assert _checkout_pins_autocrlf_false(rebuilt)


def test_unremovable_unpinned_checkout_is_not_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, sha = _lf_origin(tmp_path)
    _apply_hostile_host_env(tmp_path, monkeypatch)

    cache = GitCache(tmp_path / "cache")
    poisoned = cache.get_checkout(str(origin), sha, locked_sha=sha)
    _poison_autocrlf_pin(poisoned)

    with (
        patch.object(cache, "_evict_checkout"),
        pytest.raises(RuntimeError, match=r"Failed to rematerialize unpinned git checkout"),
    ):
        cache.get_checkout(str(origin), sha, locked_sha=sha)


def test_pin_reads_core_section_not_url_substring(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    git_dir = checkout / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        '[remote "origin"]\n\turl = https://example.com/autocrlf=false.git\n',
        encoding="utf-8",
    )
    assert not _checkout_pins_autocrlf_false(checkout)


def test_pin_treats_non_utf8_config_as_missing(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    git_dir = checkout / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_bytes(b"\xff\xfe[core]\n\tautocrlf = false\n")
    assert not _checkout_pins_autocrlf_false(checkout)
