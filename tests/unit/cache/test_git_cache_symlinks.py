"""Real-git regression: cached checkouts pin ``core.symlinks=true`` (#3457).

APM's auth environment hides the user's global Git config. Git for Windows
then records ``core.symlinks=false`` and writes each in-repo link as a text
file, so the package hash differs from the POSIX hash of the same commit.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.cache import git_cache as git_cache_module
from apm_cli.cache.git_cache import (
    GitCache,
    _checkout_has_materialization_pins,
    _safe_git_args,
)
from apm_cli.deps.bare_cache import materialize_from_bare
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.file_ops import robust_copytree
from apm_cli.utils.git_env import GitSymlinkCapabilityError, get_git_executable

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]

_BODY = b"Package instructions\n"


def _neutral_git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env.pop("GIT_CONFIG_NOSYSTEM", None)
    env.pop("GIT_CONFIG_PARAMETERS", None)
    env.pop("GIT_CONFIG_COUNT", None)
    for key in list(env):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key, None)
    return env


def _git(args: list[str], *, env: dict[str, str], input: str | None = None) -> str:
    return subprocess.run(
        [get_git_executable(), *args],
        env=env,
        input=input,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def _symlink_origin(tmp_path: Path) -> tuple[Path, str]:
    """Commit ``pkg/AGENTS.md -> CLAUDE.md`` without needing OS link privileges."""
    origin = tmp_path / "origin"
    env = _neutral_git_env()
    _git(["init", "-q", "-b", "main", "--template=", str(origin)], env=env)
    _git(["-C", str(origin), "config", "user.email", "test@example.com"], env=env)
    _git(["-C", str(origin), "config", "user.name", "APM Test"], env=env)
    (origin / "pkg").mkdir()
    (origin / "pkg" / "CLAUDE.md").write_bytes(_BODY)
    _git(["-C", str(origin), "add", "pkg/CLAUDE.md"], env=env)
    oid = _git(
        ["-C", str(origin), "hash-object", "-w", "--stdin"],
        env=env,
        input="CLAUDE.md",
    )
    _git(
        ["-C", str(origin), "update-index", "--add", "--cacheinfo", "120000", oid, "pkg/AGENTS.md"],
        env=env,
    )
    _git(["-C", str(origin), "commit", "-q", "-m", "symlink fixture"], env=env)
    return origin, _git(["-C", str(origin), "rev-parse", "HEAD"], env=env)


def _caller_env_freezing_false() -> dict[str, str]:
    """Mimic an isolated caller env whose frozen config disables symlinks."""
    env = _neutral_git_env()
    env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.symlinks", GIT_CONFIG_VALUE_0="false")
    return env


def _host_creates_symlinks(scratch: Path) -> bool:
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "target.txt").write_bytes(b"probe")
    try:
        os.symlink(scratch / "target.txt", scratch / "link.txt")
    except OSError:
        return False
    return True


def _posix_hash(tmp_path: Path) -> str:
    expected = tmp_path / "posix-expected"
    expected.mkdir()
    (expected / "AGENTS.md").write_bytes(_BODY)
    (expected / "CLAUDE.md").write_bytes(_BODY)
    return compute_package_hash(expected)


def test_safe_git_args_pin_real_symlinks() -> None:
    args = _safe_git_args()
    assert "core.symlinks=true" in args
    assert args[args.index("core.symlinks=true") - 1] == "-c"


@pytest.mark.parametrize("sparse_paths", [None, ["pkg"]])
def test_cached_checkout_materializes_links_with_posix_hash(
    tmp_path: Path, sparse_paths: list[str] | None
) -> None:
    origin, sha = _symlink_origin(tmp_path)
    cache = GitCache(tmp_path / "cache")

    if not _host_creates_symlinks(tmp_path / "capability"):
        with pytest.raises(GitSymlinkCapabilityError):
            cache.get_checkout(
                str(origin),
                sha,
                locked_sha=sha,
                env=_caller_env_freezing_false(),
                sparse_paths=sparse_paths,
            )
        return

    checkout = cache.get_checkout(
        str(origin),
        sha,
        locked_sha=sha,
        env=_caller_env_freezing_false(),
        sparse_paths=sparse_paths,
    )
    installed = tmp_path / "apm_modules" / "pkg"
    robust_copytree(checkout / "pkg", installed)

    assert (checkout / "pkg" / "AGENTS.md").is_symlink()
    assert _checkout_has_materialization_pins(checkout)
    assert compute_package_hash(installed) == _posix_hash(tmp_path)


def test_cache_hit_rematerializes_shard_missing_symlink_pin(tmp_path: Path) -> None:
    """Shards written before the pin may hold link text; heal them on hit."""
    origin, sha = _symlink_origin(tmp_path)
    if not _host_creates_symlinks(tmp_path / "capability"):
        pytest.skip("Rematerialization needs a host that can create symlinks")
    cache = GitCache(tmp_path / "cache")
    stale = cache.get_checkout(str(origin), sha, locked_sha=sha)
    link = stale / "pkg" / "AGENTS.md"
    link.unlink()
    link.write_bytes(b"CLAUDE.md")
    config = stale / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8").replace("symlinks = true", "symlinks = false"),
        encoding="utf-8",
    )
    assert not _checkout_has_materialization_pins(stale)

    healed = cache.get_checkout(str(origin), sha, locked_sha=sha)

    assert (healed / "pkg" / "AGENTS.md").is_symlink()
    assert _checkout_has_materialization_pins(healed)


def test_pins_require_both_autocrlf_and_symlinks(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    config = checkout / ".git" / "config"
    config.write_text("[core]\n\tautocrlf = false\n", encoding="utf-8")
    assert not _checkout_has_materialization_pins(checkout)
    config.write_text("[core]\n\tautocrlf = false\n\tsymlinks = true\n", encoding="utf-8")
    assert _checkout_has_materialization_pins(checkout)
    config.write_text("[core]\n\tsymlinks = true\n", encoding="utf-8")
    assert not _checkout_has_materialization_pins(checkout)


def test_link_creation_failure_raises_diagnostic_and_cleans_stage(tmp_path: Path) -> None:
    """Git's ``unable to create symlink`` becomes the named capability error."""
    origin, sha = _symlink_origin(tmp_path)
    cache = GitCache(tmp_path / "cache")
    real_run = subprocess.run

    def run(args: list[str], *pargs: object, **kwargs: object) -> subprocess.CompletedProcess:
        if "checkout" in args and sha in args:
            raise subprocess.CalledProcessError(
                128,
                args,
                stderr="error: unable to create symlink pkg/AGENTS.md: Permission denied\n",
            )
        return real_run(args, *pargs, **kwargs)

    with (
        patch.object(git_cache_module.subprocess, "run", side_effect=run),
        pytest.raises(GitSymlinkCapabilityError) as raised,
    ):
        cache.get_checkout(str(origin), sha, locked_sha=sha)

    assert str(origin.name) in str(raised.value)
    shard_root = tmp_path / "cache" / "checkouts_v1"
    leftovers = [p for p in shard_root.rglob("*") if ".incomplete." in p.name]
    assert leftovers == []


def test_bare_materialization_pins_symlinks_over_caller_env(tmp_path: Path) -> None:
    """The shared-bare consumer checkout path carries the same pin."""
    origin, sha = _symlink_origin(tmp_path)
    bare = tmp_path / "bare.git"
    _git(["clone", "-q", "--bare", "--template=", str(origin), str(bare)], env=_neutral_git_env())
    consumer = tmp_path / "consumer"

    if not _host_creates_symlinks(tmp_path / "capability"):
        with pytest.raises(GitSymlinkCapabilityError):
            materialize_from_bare(
                bare,
                consumer,
                ref=sha,
                env=_caller_env_freezing_false(),
                known_sha=sha,
                sparse_paths=["pkg"],
                package="owner/repo",
            )
        return

    materialize_from_bare(
        bare,
        consumer,
        ref=sha,
        env=_caller_env_freezing_false(),
        known_sha=sha,
        sparse_paths=["pkg"],
        package="owner/repo",
    )

    assert (consumer / "pkg" / "AGENTS.md").is_symlink()
