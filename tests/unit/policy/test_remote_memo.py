"""One live ``git remote`` read per command run (policy/_remote.py over core/invocation_memo).

Org policy discovery runs several times in one ``apm install`` (preflight,
policy gate, target check, MCP) and each shells out to ``git remote``. The
CLI opens one invocation scope per command; outside it every read is live.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.core.invocation_memo import invocation_scope
from apm_cli.policy import _remote

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-remote-origin-reader")]

_URL = "https://github.com/contoso/app.git"


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv[1:]))
        stdout = _URL + "\n" if list(argv[1:3]) == ["remote", "get-url"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_reads_are_live_outside_a_command_run(tmp_path: Path, git_calls) -> None:
    for _ in range(3):
        assert _remote._git_remote_origin_url(tmp_path) == _URL
    assert git_calls == [("remote", "get-url", "origin")] * 3


def test_one_command_run_reads_each_remote_once(tmp_path: Path, git_calls) -> None:
    other = tmp_path / "other"
    with invocation_scope():
        for _ in range(4):
            _remote._extract_org_host_port_from_git_remote(tmp_path)
        _remote._git_remote_origin_url(other)
    assert git_calls == [("remote", "get-url", "origin")] * 2
    with invocation_scope():
        _remote._git_remote_origin_url(tmp_path)
    assert len(git_calls) == 3, "the next command run must read the remote live again"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_the_memo_key_keeps_parent_components(tmp_path: Path, git_calls) -> None:
    """'..' is not collapsed: apparent/link/.. and apparent are different directories."""
    (tmp_path / "physical" / "child").mkdir(parents=True)
    (tmp_path / "apparent").mkdir()
    (tmp_path / "apparent" / "link").symlink_to(tmp_path / "physical" / "child")
    with invocation_scope():
        _remote._git_remote_origin_url(tmp_path / "apparent" / "link" / "..")
        _remote._git_remote_origin_url(tmp_path / "apparent")
    assert len(git_calls) == 2, "two distinct directories must each be read"


def test_a_failed_read_is_not_retried_within_the_run(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def no_remote(argv, **_kwargs):
        calls.append(tuple(argv[1:]))
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="no such remote")

    monkeypatch.setattr(subprocess, "run", no_remote)
    with invocation_scope():
        assert _remote._git_remote_origin_url(tmp_path) is None
        assert _remote._git_remote_origin_url(tmp_path) is None
    assert calls == [("remote", "get-url", "origin"), ("remote",)]
