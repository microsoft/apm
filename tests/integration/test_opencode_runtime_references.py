"""Installed CLI renders native OpenCode references without persisting values."""

import copy
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]


def test_installed_cli_opencode_runtime_references(tmp_path: Path, apm_binary_path: Path):
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root / "project"
    project.mkdir()
    (project / ".opencode").mkdir()
    dump_yaml(
        {
            "name": "runtime-references",
            "version": "1.0.0",
            "targets": ["opencode"],
            "dependencies": {
                "apm": [],
                "mcp": [
                    {
                        "name": "local",
                        "registry": False,
                        "transport": "stdio",
                        "command": "echo",
                        "args": ["${EXAMPLE_KEY}"],
                        "env": {"TOKEN": "${env:EXAMPLE_KEY}", "STATIC": "literal"},
                    },
                    {
                        "name": "remote",
                        "registry": False,
                        "transport": "http",
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer ${EXAMPLE_KEY}"},
                    },
                ],
            },
        },
        project / "apm.yml",
    )
    LockFile().write(project / "apm.lock.yaml")
    environment = isolated.subprocess_env(overrides={"EXAMPLE_KEY": "synthetic-cli-secret"})
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--no-policy", "--target", "opencode"),
        cwd=project,
        env=environment,
        scenario_id="opencode-runtime-references",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    written = (project / "opencode.json").read_text(encoding="utf-8")
    assert "synthetic-cli-secret" not in written
    config = json.loads(written)["mcp"]
    assert config["local"]["command"] == ["echo", "{env:EXAMPLE_KEY}"]
    assert config["local"]["environment"] == {"TOKEN": "{env:EXAMPLE_KEY}", "STATIC": "literal"}
    assert config["remote"]["headers"]["Authorization"] == "Bearer {env:EXAMPLE_KEY}"


def test_installed_cli_registry_authored_environment_overlay(tmp_path: Path, apm_binary_path: Path):
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "registry-overlay", base_env=dict(os.environ)
    )
    project = isolated.work_root / "project"
    project.mkdir()
    (project / ".opencode").mkdir()
    name = "com.example/runtime-overlay"
    server = {
        "name": name,
        "description": "Synthetic registry fixture",
        "version": "1.0.0",
        "packages": [
            {
                "registry_name": "npm",
                "name": "example-mcp",
                "version": "1.0.0",
                "environment_variables": [
                    {"name": "SERVER_MODE", "required": True},
                    {"name": "GITHUB_TOOLSETS", "required": True},
                    {"name": "TOKEN", "required": True},
                ],
            }
        ],
    }
    second_server = copy.deepcopy(server)
    second_server["name"] = "com.example/second-overlay"
    factory = LocalMcpRegistryFactory(isolated.root / "registries")
    with factory.start(server) as registry, factory.start(second_server) as second_registry:
        dump_yaml(
            {
                "name": "registry-runtime-overlay",
                "version": "1.0.0",
                "targets": ["opencode"],
                "dependencies": {
                    "apm": [],
                    "mcp": [
                        {
                            "name": name,
                            "registry": registry.url,
                            "env": {
                                "SERVER_MODE": "development",
                                "GITHUB_TOOLSETS": "issues",
                                "TOKEN": "${OTHER_TOKEN}",
                                "EXTRA": "literal",
                            },
                        },
                        {
                            "name": second_server["name"],
                            "registry": second_registry.url,
                            "env": {
                                "SERVER_MODE": "production",
                                "GITHUB_TOOLSETS": "repos",
                                "TOKEN": "${SECOND_TOKEN}",
                            },
                        },
                    ],
                },
            },
            project / "apm.yml",
        )
        LockFile().write(project / "apm.lock.yaml")
        environment = isolated.subprocess_env(
            overrides={
                "OTHER_TOKEN": "synthetic-other-secret",
                "TOKEN": "synthetic-wrong-secret",
                "SECOND_TOKEN": "synthetic-second-secret",
                "MCP_REGISTRY_ALLOW_HTTP": "1",
            }
        )
        environment["APM_TEST_LOOPBACK_PORTS"] = ",".join(
            str(urlparse(endpoint.url).port) for endpoint in (registry, second_registry)
        )
        result = ApmLifecycleRunner((str(apm_binary_path),)).run(
            ("install", "--no-policy", "--target", "opencode"),
            cwd=project,
            env=environment,
            scenario_id="opencode-registry-runtime-overlay",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        written = (project / "opencode.json").read_text(encoding="utf-8")
        config = json.loads(written)["mcp"]
        assert len(config) == 2
        assert config["runtime-overlay"]["environment"] == {
            "SERVER_MODE": "development",
            "GITHUB_TOOLSETS": "issues",
            "TOKEN": "{env:OTHER_TOKEN}",
        }
        assert config["second-overlay"]["environment"] == {
            "SERVER_MODE": "production",
            "GITHUB_TOOLSETS": "repos",
            "TOKEN": "{env:SECOND_TOKEN}",
        }
        assert "synthetic-other-secret" not in written
        assert "synthetic-wrong-secret" not in written
        assert "synthetic-second-secret" not in written
        assert registry.request_paths
        assert second_registry.request_paths

        first_lock = (project / "apm.lock.yaml").read_bytes()
        assert b"_apm_env_overlay" not in first_lock
        repeated = ApmLifecycleRunner((str(apm_binary_path),)).run(
            ("install", "--no-policy", "--target", "opencode"),
            cwd=project,
            env=environment,
            scenario_id="opencode-registry-runtime-overlay-repeat",
        )
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert (project / "opencode.json").read_text(encoding="utf-8") == written
        assert (project / "apm.lock.yaml").read_bytes() == first_lock
