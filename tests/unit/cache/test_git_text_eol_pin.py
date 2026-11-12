"""Real-git regression: host ``core.eol`` must not change package bytes (apm#3270).

Under ``.gitattributes`` ``text`` / ``text=auto``, Git writes the line ending
that ``core.eol`` selects. Its default ``native`` is CRLF on Windows, and a
host may also set it in system, global or process config. Every supported
materialization path must write the committed LF bytes on every host, while
explicit ``eol=`` attributes, binary paths and committed CRLF blobs keep their
repository-defined bytes.

The ``host-default`` case sets no ``core.eol`` at all: on Windows it is the
native-CRLF control; elsewhere it is the LF control.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.cache.git_cache import GitCache, _checkout_has_materialization_pins
from apm_cli.deps.bare_cache import materialize_from_bare
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.git_env import (
    checkout_git_worktree,
    clone_git_worktree,
    get_git_executable,
    git_network_env,
    reset_git_cache,
)

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]

_ATTRIBUTES = b"* text=auto\n*.bat text eol=crlf\n*.sh text eol=lf\n*.bin binary\n"

# Bytes every host must materialize at the pinned commit.
_EXPECTED: dict[str, bytes] = {
    "pkg/.apm/skills/demo/SKILL.md": b"---\nname: demo\n---\n# Demo\n\nLine one.\nLine two.\n",
    "pkg/apm.yml": b"name: pkg\nversion: 1.0.0\n",
    "pkg/scripts/run.bat": b"@echo off\r\necho hi\r\n",
    "pkg/scripts/run.sh": b"#!/bin/sh\necho hi\n",
    "pkg/assets/blob.bin": b"\x00\x01\r\n\x02\n",
    "pkg/legacy/committed-crlf.md": b"# Legacy\r\nkept as committed\r\n",
}
_HOST_SELECTED = "pkg/.apm/skills/demo/SKILL.md"


def _neutral_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_CONFIG")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def _git(*args: str, cwd: Path, env: dict[str, str]) -> str:
    return subprocess.run(
        [get_git_executable(), "-c", "core.autocrlf=false", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _write(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, str]:
    """Publish a text=auto package; the CRLF blob predates the attributes."""
    src = tmp_path / "origin"
    src.mkdir()
    env = _neutral_env()
    ident = ("-c", "user.name=APM Test", "-c", "user.email=test@example.com")
    _git("init", "-q", "-b", "main", ".", cwd=src, env=env)
    legacy = "pkg/legacy/committed-crlf.md"
    _write(src, legacy, _EXPECTED[legacy])
    _git("add", legacy, cwd=src, env=env)
    _git(*ident, "commit", "-qm", "legacy crlf", cwd=src, env=env)
    _write(src, ".gitattributes", _ATTRIBUTES)
    for rel, data in _EXPECTED.items():
        if rel == "pkg/scripts/run.bat":
            data = data.replace(b"\r\n", b"\n")
        if rel != legacy:
            _write(src, rel, data)
    _git(
        "add",
        ".gitattributes",
        "pkg/.apm",
        "pkg/apm.yml",
        "pkg/scripts",
        "pkg/assets",
        cwd=src,
        env=env,
    )
    _git(*ident, "commit", "-qm", "text=auto package", cwd=src, env=env)
    eol = _git("ls-files", "--eol", cwd=src, env=env)
    assert "i/crlf" in eol and "pkg/legacy/committed-crlf.md" in eol
    assert "i/lf" in eol and "attr/text=auto" in eol
    return src, _git("rev-parse", "HEAD", cwd=src, env=env)


def _host_eol_crlf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """Install a host ``core.eol=crlf`` through one config source."""
    for key in list(os.environ):
        if key.upper().startswith("GIT_CONFIG"):
            monkeypatch.delenv(key, raising=False)
    system = tmp_path / "system.gitconfig"
    system.write_bytes(b"[safe]\n\tbareRepository = all\n")
    global_ = tmp_path / "global.gitconfig"
    global_.write_bytes(b"")
    crlf = b"[core]\n\teol = crlf\n"
    if source == "system":
        system.write_bytes(system.read_bytes() + crlf)
    elif source == "global":
        global_.write_bytes(crlf)
    elif source == "env-count":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.eol")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "crlf")
    elif source == "env-parameters":
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.eol'='crlf'")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_))
    reset_git_cache()
    if source != "host-default":
        observed = subprocess.run(
            [get_git_executable(), "config", "--get", "core.eol"],
            env=dict(os.environ),
            capture_output=True,
            text=True,
            check=True,
        )
        assert observed.stdout.strip() == "crlf"


_SOURCES = ["host-default", "system", "global", "env-count", "env-parameters"]


def _assert_repository_bytes(root: Path, rels: list[str] | None = None) -> None:
    for rel in rels or list(_EXPECTED):
        assert (root / rel).read_bytes() == _EXPECTED[rel], rel


def _expected_package_hash(tmp_path: Path) -> str:
    reference = tmp_path / "expected"
    for rel, data in _EXPECTED.items():
        _write(reference, rel, data)
    return compute_package_hash(reference / "pkg")


@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.parametrize("sparse", [False, True], ids=["full", "sparse"])
def test_cached_checkout_ignores_host_eol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: tuple[Path, str],
    source: str,
    sparse: bool,
) -> None:
    src, sha = origin
    _host_eol_crlf(tmp_path, monkeypatch, source)

    checkout = GitCache(tmp_path / "cache").get_checkout(
        str(src), sha, locked_sha=sha, sparse_paths=["pkg"] if sparse else None
    )

    _assert_repository_bytes(checkout)
    assert compute_package_hash(checkout / "pkg") == _expected_package_hash(tmp_path)
    assert _checkout_has_materialization_pins(checkout)


@pytest.mark.parametrize("source", ["host-default", "system", "env-count"])
@pytest.mark.parametrize("sparse", [False, True], ids=["full", "sparse"])
def test_bare_materialization_ignores_host_eol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: tuple[Path, str],
    source: str,
    sparse: bool,
) -> None:
    src, sha = origin
    bare = tmp_path / "bare.git"
    _git("clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path, env=_neutral_env())
    _host_eol_crlf(tmp_path, monkeypatch, source)

    consumer = tmp_path / "consumer"
    resolved = materialize_from_bare(
        bare,
        consumer,
        ref=sha,
        env=dict(os.environ),
        known_sha=sha,
        sparse_paths=["pkg"] if sparse else None,
    )

    assert resolved == sha
    _assert_repository_bytes(consumer)
    # The consumer records the pins for later Git commands in it, written by
    # ``clone --config`` rather than one ``git config`` spawn per key.
    persisted = {
        key: _git(
            "config",
            "--file",
            str(consumer / ".git" / "config"),
            key,
            cwd=tmp_path,
            env=_neutral_env(),
        )
        for key in ("core.autocrlf", "core.eol")
    }
    assert persisted == {"core.autocrlf": "false", "core.eol": "lf"}


@pytest.mark.parametrize("source", ["host-default", "global", "env-count"])
def test_uncached_fallback_clone_and_checkout_ignore_host_eol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: tuple[Path, str],
    source: str,
) -> None:
    src, sha = origin
    _host_eol_crlf(tmp_path, monkeypatch, source)
    target = tmp_path / "worktree"

    clone_git_worktree(src.resolve().as_uri(), target, env=dict(os.environ))
    _assert_repository_bytes(target)
    checkout_git_worktree(target, sha, env=dict(os.environ))

    _assert_repository_bytes(target)


def test_network_env_replaces_host_line_ending_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: tuple[Path, str]
) -> None:
    """Snapshot children without ``-c`` pins (sparse fallback, file transport) stay LF."""
    src, _sha = origin
    _host_eol_crlf(tmp_path, monkeypatch, "global")
    global_config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    global_config.write_bytes(b"[core]\n\teol = crlf\n\tautocrlf = true\n")

    env = git_network_env(src.resolve().as_uri(), dict(os.environ))

    entries = [
        (env[f"GIT_CONFIG_KEY_{index}"].lower(), env[f"GIT_CONFIG_VALUE_{index}"])
        for index in range(int(env["GIT_CONFIG_COUNT"]))
    ]
    assert ("core.eol", "lf") in entries
    assert ("core.autocrlf", "false") in entries
    assert ("core.eol", "crlf") not in entries
    assert ("core.autocrlf", "true") not in entries

    target = tmp_path / "plain"
    subprocess.run(
        [get_git_executable(), "clone", "-q", src.resolve().as_uri(), str(target)],
        env=env,
        capture_output=True,
        check=True,
    )
    _assert_repository_bytes(target)


def _make_legacy_shard(checkout: Path) -> None:
    """Rewrite a shard as a pre-#3270 Windows cache entry would look."""
    path = checkout / _HOST_SELECTED
    path.write_bytes(_EXPECTED[_HOST_SELECTED].replace(b"\n", b"\r\n"))
    config = checkout / ".git" / "config"
    text = config.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip().lower() != "eol = lf"]
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("sparse", [False, True], ids=["full", "sparse"])
def test_legacy_shard_without_eol_pin_is_rematerialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: tuple[Path, str],
    sparse: bool,
) -> None:
    src, sha = origin
    _host_eol_crlf(tmp_path, monkeypatch, "system")
    cache = GitCache(tmp_path / "cache")
    sparse_paths = ["pkg"] if sparse else None
    shard = cache.get_checkout(str(src), sha, locked_sha=sha, sparse_paths=sparse_paths)
    _make_legacy_shard(shard)
    assert not _checkout_has_materialization_pins(shard)

    healed = cache.get_checkout(str(src), sha, locked_sha=sha, sparse_paths=sparse_paths)

    assert healed == shard
    _assert_repository_bytes(healed)
    assert _checkout_has_materialization_pins(healed)
    assert _git("rev-parse", "HEAD", cwd=healed, env=_neutral_env()) == sha


def test_bare_materialization_pins_line_endings_without_extra_spawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-consumer path records the pins on the clone, not via ``git config``."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("apm_cli.deps.bare_cache.subprocess.run", fake_run)
    materialize_from_bare(
        tmp_path / "bare.git", tmp_path / "consumer", ref=None, env={}, known_sha="a" * 40
    )

    clone = next(argv for argv in calls if "clone" in argv)
    clone_config = [clone[i + 1] for i, arg in enumerate(clone) if arg == "--config"]
    assert clone_config == ["core.autocrlf=false", "core.eol=lf"]
    config_keys = {argv[argv.index("config") + 1] for argv in calls if "config" in argv}
    assert not config_keys & {"core.autocrlf", "core.eol"}
