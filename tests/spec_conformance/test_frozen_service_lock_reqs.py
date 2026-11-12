"""Real-consumer proof of frozen lock immutability through late service integration."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.e2e


@pytest.mark.req("req-lk-006")
@pytest.mark.parametrize(
    ("target", "expected_exit"),
    [("copilot,claude", 0), ("claude", 0), ("copilot,claude,intellij", 1)],
    ids=["recorded", "narrower", "unrecorded"],
)
def test_frozen_service_command_preserves_committed_lock_bytes(
    tmp_path: Path, target: str, expected_exit: int
) -> None:
    """The complete frozen consumer command must not rewrite its committed lock."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "consumer", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    project = LocalPackageFactory(isolated.work_root).create(
        "frozen-lock-consumer",
        targets=("copilot", "claude"),
        mcp_dependencies=(
            {
                "name": "locked-service",
                "registry": False,
                "transport": "http",
                "url": "https://example.invalid/locked-service",
            },
        ),
        lsp_dependencies=(
            {
                "name": "locked-language",
                "command": "inert-language-server",
                "extensionToLanguage": {".fixture": "fixture"},
            },
        ),
    )
    runner = ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli"))
    args = ("install", "--no-policy")
    baseline = runner.run(args, cwd=project.root, env=environment)
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    lockfile = project.root / "apm.lock.yaml"
    committed = (
        b"# Preserve committed formatting and legacy metadata.\n"
        + lockfile.read_bytes()
        + b"generated_at: '2020-01-02T03:04:05Z'\n"
    )
    lockfile.write_bytes(committed)
    manifest = project.manifest_path.read_bytes()

    for _ in range(2):
        frozen = runner.run(
            (*args, "--frozen", "--target", target), cwd=project.root, env=environment
        )
        assert frozen.returncode == expected_exit, frozen.stdout + frozen.stderr
        assert lockfile.read_bytes() == committed
        assert project.manifest_path.read_bytes() == manifest
