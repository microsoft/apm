"""APMLifecycle: install --frozen reports a parked MCP server once (#4422).

PROMISE: I9 -- no duplicate lines: a dependency MCP server parked because its
executables are not approved is reported once per command, by the MCP phase
(source: .agents/skills/apm-lifecycle-tests, cross-cutting invariants).
CELL: OP=install FROZEN SRC=local PRIM=mcp SCOPE=project GATE=unapproved TGT=copilot
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I9"),
]

_NOTICE = "Parked MCP server 'srv' (../srvpkg): executables not approved"


def test_frozen_install_reports_a_parked_mcp_server_once(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    source = isolated.work_root / "srvpkg"
    source.mkdir(parents=True)
    dump_yaml(
        {
            "name": "srvpkg",
            "version": "1.0.0",
            "dependencies": {
                "mcp": [{"name": "srv", "registry": False, "transport": "stdio", "command": "echo"}]
            },
        },
        source / "apm.yml",
    )
    project = isolated.work_root / "proj"
    (project / ".github").mkdir(parents=True)
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": ["../srvpkg"]},
            "executables": {"allow": {}},
        },
        project / "apm.yml",
    )
    env = {**isolated.subprocess_env(), "COLUMNS": "250"}
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    def apm(*args: str) -> str:
        result = runner.run(args, cwd=project, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout + result.stderr

    assert apm("install", "--no-policy").count(_NOTICE) == 1
    frozen = apm("install", "--frozen", "--no-policy")

    assert frozen.count(_NOTICE) == 1, frozen
