"""Real Git materialization with only the external fetch boundary redirected."""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference
from apm_cli.utils.path_security import PathTraversalError
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = pytest.mark.component

_REMOTE = "ssh://git@gitlab-ssh.example.com:2222/owner/repo.git"
_PATHS = ("agents/first.agent.md", "instructions/second.instructions.md")
_OLD = (b"# old agent\r\n\x00\n", b"old instruction\n")
_NEW = (b"# new agent\n\xff\n", b"new instruction\r\n")


@pytest.fixture
def materialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Reuse isolated config/local repositories, never faking checkout or bytes."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    env = isolated.subprocess_env()
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repo = factory.create("gitlab")
    for path, content in zip(_PATHS, _OLD, strict=True):
        target = repo.worktree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    first = factory.commit(repo, message="first")
    factory.tag(repo, "v1", first)
    for path, content in zip(_PATHS, _NEW, strict=True):
        (repo.worktree / path).write_bytes(content)
    second = factory.commit(repo, message="second")
    real_run = subprocess.run
    observations: list[tuple[str, list[str], dict[str, str]]] = []
    expected = {"remote": _REMOTE}
    gate: dict[str, threading.Event] = {}

    def guarded_run(command: list[str], *args: object, **kwargs: object) -> object:
        argv = list(command)
        assert Path(argv[0]).stem == "git", f"Unexpected subprocess: {argv}"
        if "fetch" in argv:
            remote = real_run(
                [argv[0], "config", "--get", "remote.origin.url"],
                cwd=kwargs["cwd"],
                env=kwargs["env"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            parsed = urlparse(remote)
            wanted = urlparse(expected["remote"])
            assert (parsed.scheme, parsed.username, parsed.hostname, parsed.port, parsed.path) == (
                wanted.scheme,
                wanted.username,
                wanted.hostname,
                wanted.port,
                wanted.path,
            ), "P1/P2 manifest transport must reach the actual Git remote"
            assert argv[1:5] == ["fetch", "--filter=blob:none", "--depth=1", "origin"]
            assert len(argv) == 6
            assert "glpat-2938-dummy-secret" not in repr((argv, remote, kwargs["env"])), (
                "P7 credentials must not enter Git subprocess"
            )
            observations.append((remote, argv.copy(), dict(kwargs["env"])))
            if gate:
                gate["entered"].set()
                assert gate["release"].wait(10), "fetch release timed out"
            argv[4] = repo.file_url
        else:
            assert any(
                operation in argv
                for operation in (
                    "init",
                    "config",
                    "remote",
                    "sparse-checkout",
                    "checkout",
                    "rev-parse",
                )
            ) or ("ls-remote" in argv and "--get-url" in argv), f"Network command: {argv}"
        return real_run(argv, *args, **kwargs)

    with patch.dict(os.environ, env, clear=True):
        monkeypatch.setattr(tempfile, "tempdir", str(isolated.temp_root))
        monkeypatch.setenv("APM_GITLAB_HOSTS", "gitlab-ssh.example.com")
        monkeypatch.setenv("GITLAB_APM_PAT", "glpat-2938-dummy-secret")
        monkeypatch.setattr(subprocess, "run", guarded_run)
        monkeypatch.setattr(
            socket, "create_connection", Mock(side_effect=AssertionError("network forbidden"))
        )
        monkeypatch.setattr(
            socket, "getaddrinfo", Mock(side_effect=AssertionError("DNS forbidden"))
        )
        monkeypatch.setattr(socket, "socket", Mock(side_effect=AssertionError("socket forbidden")))
        monkeypatch.setattr(
            "requests.sessions.Session.request",
            Mock(side_effect=AssertionError("HTTP forbidden")),
        )
        owner = GitHubPackageDownloader(
            auth_resolver=AuthResolver(allow_external_fallback=False), allow_fallback=False
        )
        api = Mock(side_effect=AssertionError("P3/P8 REST must not follow SSH/local Git"))
        monkeypatch.setattr(owner, "_resilient_get", api)
        yield {
            "owner": owner,
            "first": first.sha,
            "second": second.sha,
            "observations": observations,
            "api": api,
            "gate": gate,
            "factory": factory,
            "repo": repo,
            "expected": expected,
        }
        owner._strategies._git_file_transport_finalizer()


def _fetch(state: dict, path: str, ref: str, url: str = _REMOTE) -> bytes:
    """Exercise public downloader routing with the real manifest object."""
    dep = DependencyReference.parse_from_dict({"git": url, "path": path, "type": "gitlab"})
    return state["owner"]._download_github_file(dep, path, ref)


@pytest.mark.parametrize("ref_name", ["main", "v1", "sha"])
def test_real_git_manifest_remote_bytes_and_single_fetch(
    materialization: dict, ref_name: str
) -> None:
    """P1/P7/P9: actual remote fidelity plus exact branch/tag/SHA materialization."""
    state = materialization
    ref = state["first"] if ref_name == "sha" else ref_name
    expected = _NEW if ref_name == "main" else _OLD
    assert tuple(_fetch(state, path, ref) for path in _PATHS) == expected, (
        "P9 requested ref must materialize exact local Git bytes"
    )
    assert len(state["observations"]) == 1
    assert state["observations"][0][1][-1] == ref
    transports = list(state["owner"]._strategies._git_file_transports.values())
    assert len(transports) == 1
    checkout = transports[0]._work_dir
    assert checkout.is_dir()
    state["api"].assert_not_called()
    state["owner"]._strategies._git_file_transport_finalizer()
    assert not checkout.exists()


def test_missing_custom_ref_is_not_replaced(materialization: dict) -> None:
    """P9: failure never silently materializes main or master instead."""
    with pytest.raises(RuntimeError, match="missing-release"):
        _fetch(materialization, _PATHS[0], "missing-release")
    assert [entry[1][-1] for entry in materialization["observations"]] == ["missing-release"]
    assert materialization["owner"]._strategies._git_file_transports == {}
    materialization["api"].assert_not_called()


def test_concurrent_paths_share_one_initial_checkout(materialization: dict) -> None:
    """P10: bounded barriers overlap callers without timing-sensitive sleeps."""
    state = materialization
    state["gate"].update(entered=threading.Event(), release=threading.Event())
    start = threading.Barrier(3, timeout=10)
    results: dict[str, bytes] = {}
    failures: list[Exception] = []

    def worker(path: str) -> None:
        try:
            start.wait()
            results[path] = _fetch(state, path, "main")
        except Exception as exc:
            failures.append(exc)

    workers = [threading.Thread(target=worker, args=(path,), daemon=True) for path in _PATHS]
    for worker_thread in workers:
        worker_thread.start()
    try:
        start.wait()
        assert state["gate"]["entered"].wait(10), "no caller reached fetch"
    finally:
        state["gate"]["release"].set()
        for worker_thread in workers:
            worker_thread.join(timeout=15)
    assert all(not worker_thread.is_alive() for worker_thread in workers)
    assert failures == []
    assert results == dict(zip(_PATHS, _NEW, strict=True))
    assert len(state["observations"]) == 1
    assert _fetch(state, _PATHS[0], "main") == _NEW[0]
    assert len(state["observations"]) == 1
    state["api"].assert_not_called()


def test_traversal_is_terminal_before_fetch(materialization: dict) -> None:
    """P6: path validation remains real and cannot advance an opted-in plan."""
    materialization["owner"]._allow_fallback = True
    dep = DependencyReference.parse_from_dict({"git": _REMOTE, "type": "gitlab"})
    with pytest.raises(PathTraversalError):
        materialization["owner"]._download_github_file(dep, "../outside.md", "main")
    assert materialization["observations"] == []
    materialization["api"].assert_not_called()


def test_local_read_failure_is_terminal(
    materialization: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P6: a real materialized file's I/O failure cannot become a Git retry."""
    state = materialization
    state["owner"]._allow_fallback = True
    read_bytes = Path.read_bytes

    def failed_read(path: Path) -> bytes:
        if path.name == "first.agent.md":
            raise OSError("simulated unreadable materialized file")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", failed_read)
    with pytest.raises(OSError, match="unreadable materialized"):
        _fetch(state, _PATHS[0], "main")
    assert len(state["observations"]) == 1
    state["api"].assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="Creating symlinks requires Windows privileges")
def test_cached_symlink_escape_is_terminal(materialization: dict) -> None:
    """P6: real containment is rechecked before reading even an existing checkout."""
    state = materialization
    assert _fetch(state, _PATHS[0], "main") == _NEW[0]
    transport = next(iter(state["owner"]._strategies._git_file_transports.values()))
    target = transport._work_dir / _PATHS[0]
    target.unlink()
    target.symlink_to(transport._work_dir.parent / "outside.md")
    state["owner"]._allow_fallback = True
    with pytest.raises(PathTraversalError):
        _fetch(state, _PATHS[0], "main")
    assert len(state["observations"]) == 1
    state["api"].assert_not_called()


def test_effective_local_rewrite_does_not_authorize_rest(materialization: dict) -> None:
    """P8: real Git rewrite policy sees a local mirror, not nominal HTTPS."""
    state = materialization
    url = "https://gitlab.com/owner/repo.git"
    state["factory"].install_url_rewrite(state["repo"], url)
    state["expected"]["remote"] = url
    failure = None
    try:
        _fetch(state, _PATHS[0], "missing-release", url=url)
    except Exception as exc:
        failure = exc
    assert state["api"].call_count == 0, "P8 effective rewrite must not authorize REST"
    assert isinstance(failure, RuntimeError), "P8 effective rewrite must preserve Git failure"
    assert "missing-release" in str(failure)
    assert len(state["observations"]) == 1
