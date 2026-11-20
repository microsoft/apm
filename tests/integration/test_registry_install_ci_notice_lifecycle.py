"""APMLifecycle: a registry MCP install in CI prints no bare environment notice (#4320).

PROMISE: I9 -- no duplicate or noisy lines; every line carries a status prefix
(source: .agents/skills/apm-lifecycle-tests, cross-cutting invariants).
CELL: OP=install SRC=registry PRIM=mcp SCOPE=project TGT=copilot NET=loopback-registry
"""

from __future__ import annotations

import os
from contextlib import ExitStack
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


def _server(name: str) -> dict:
    return {
        "name": f"io.github.apm/{name}",
        "description": f"{name} fixture",
        "version": "1.0.0",
        "packages": [
            {
                "registryType": "npm",
                "identifier": f"@apm/{name}",
                "transport": {"type": "stdio"},
                "environmentVariables": [{"name": "REGION", "required": True, "value": "eu"}],
            }
        ],
    }


@pytest.mark.parametrize("mode", ["CI", "APM_E2E_TESTS"])
def test_registry_install_prints_no_bare_environment_notice(
    tmp_path: Path, apm_binary_path: Path, mode: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    project = isolated.work_root / "project"
    (project / ".github").mkdir(parents=True)
    env = isolated.subprocess_env()
    for name in ("CI", "GITHUB_ACTIONS", "TRAVIS", "JENKINS_URL", "BUILDKITE", "APM_E2E_TESTS"):
        env.pop(name, None)
    env[mode] = "1"
    env["MCP_REGISTRY_ALLOW_HTTP"] = "1"
    factory = LocalMcpRegistryFactory(isolated.root / "registries")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    with ExitStack() as stack:
        registries = [stack.enter_context(factory.start(_server(n))) for n in ("alpha", "beta")]
        env["APM_TEST_LOOPBACK_PORTS"] = ",".join(
            str(urlparse(registry.url).port) for registry in registries
        )
        manifest = {
            "name": "ci-notice",
            "version": "1.0.0",
            "dependencies": {
                "apm": [],
                "mcp": [
                    {"name": f"io.github.apm/{n}", "registry": registry.url}
                    for n, registry in zip(("alpha", "beta"), registries, strict=True)
                ],
            },
        }
        (project / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        result = runner.run(
            ("install", "--only", "mcp", "--no-policy", "--target", "copilot"),
            cwd=project,
            env=env,
        )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "environment detected" not in output
    assert "test mode detected" not in output
