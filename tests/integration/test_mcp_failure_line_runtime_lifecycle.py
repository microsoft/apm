"""APMLifecycle: an MCP install failure line names its runtime (#4288).

PROMISE: I9 -- no duplicate or noisy lines: a server that fails for two
runtimes is reported once per runtime, each line naming it
(source: .agents/skills/apm-lifecycle-tests, cross-cutting invariants).
CELL: OP=install SRC=registry PRIM=mcp SCOPE=project TGT=copilot,cursor FAULT=malformed-registry-type
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I9"),
]


_SERVER = {
    "name": "com.example.lifecycle/alpha-srv",
    "description": "Malformed registry type fixture",
    "version": "1.0.0",
    "packages": [
        {
            "registryType": ["pypi"],
            "identifier": "alpha-srv",
            "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
            "runtimeArguments": [],
        }
    ],
}


def test_mcp_failure_line_names_each_runtime(tmp_path: Path, apm_binary_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    project = isolated.work_root / "project"
    (project / ".github").mkdir(parents=True)
    (project / ".cursor").mkdir()
    env = isolated.subprocess_env()
    env["MCP_REGISTRY_ALLOW_HTTP"] = "1"
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    with LocalMcpRegistryFactory(isolated.root / "registries").start(_SERVER) as registry:
        env["APM_TEST_LOOPBACK_PORTS"] = str(urlparse(registry.url).port)
        manifest = {
            "name": "mcp-failure",
            "version": "1.0.0",
            "dependencies": {
                "apm": [],
                "mcp": [{"name": _SERVER["name"], "registry": registry.url}],
            },
        }
        (project / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        result = runner.run(
            ("install", "--only", "mcp", "--no-policy", "--target", "copilot,cursor"),
            cwd=project,
            env=env,
        )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    failures = [line.strip() for line in output.splitlines() if "Failed to install" in line]
    assert len(failures) == 2, output
    assert len(set(failures)) == 2, failures
    assert any("Cursor" in line for line in failures), failures
    assert any("Copilot" in line for line in failures), failures
