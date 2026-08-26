"""Regression tests for the installed-binary Git credential shim."""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from tests.utils.git_credential_shim import GitCredentialShimFactory


@pytest.mark.windows_compat
def test_git_shim_streams_bidirectional_commands(tmp_path: Path) -> None:
    """Long-lived Git commands receive responses before their stdin closes."""
    real_git = shutil.which("git")
    assert real_git is not None

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        (real_git, "init", "--quiet"),
        cwd=repository,
        check=True,
    )
    shim = GitCredentialShimFactory(tmp_path / "shim").create(
        base_env=dict(os.environ),
        real_git=Path(real_git),
        remote_map={},
    )
    shim_git = shutil.which("git", path=shim.environment["PATH"])
    assert shim_git is not None

    process = subprocess.Popen(
        (shim_git, "cat-file", "--batch-check"),
        cwd=repository,
        env=shim.environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    response: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(
        target=lambda: response.put(process.stdout.readline()),
        daemon=True,
    )
    reader.start()

    try:
        process.stdin.write("HEAD\n")
        process.stdin.flush()
        assert response.get(timeout=5) == "HEAD missing\n"
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
        reader.join(timeout=5)
