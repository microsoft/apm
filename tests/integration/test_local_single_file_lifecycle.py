"""Installed-CLI contracts for local single-file dependency materialization."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]


def test_reported_local_agent_file_installs(tmp_path: Path, apm_binary_path: Path) -> None:
    """An existing ./ai/agents/xyz.agent.md is a dependency, not a missing directory."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "local-agent", base_env=os.environ)
    project = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        dependencies=("./ai/agents/xyz.agent.md",),
        targets=("copilot",),
    )
    source = project.root / "ai" / "agents" / "xyz.agent.md"
    source.parent.mkdir(parents=True)
    content = b"---\ndescription: Local agent regression\n---\n# XYZ\nUse local guidance.\n"
    source.write_bytes(content)
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    result = runner.run(
        ("install", "--target", "copilot", "--no-policy"),
        scenario_id="issue-3258-existing-local-agent",
        cwd=project.root,
        env=isolated.subprocess_env(),
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    deployed = project.root / ".github" / "agents" / "xyz.agent.md"
    assert deployed.is_file()
    assert "Use local guidance." in deployed.read_text(encoding="utf-8")
    assert source.read_bytes() == content
