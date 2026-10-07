"""Real CLI lifecycle coverage for OpenCode-only enabled manifest intent."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import pytest
import tomlkit

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]


def test_opencode_enabled_install_reinstall_and_scope(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Both server sources preserve JSON type through changes and stable repeats."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root / "project"
    project.mkdir()
    (project / ".opencode").mkdir()
    (project / ".vscode").mkdir()
    config_path = project / "opencode.json"
    user_server = {"type": "local", "command": ["user-server"], "enabled": False}
    config_path.write_text(
        json.dumps({"theme": "user-theme", "mcp": {"unowned": user_server}}), encoding="utf-8"
    )
    registry_factory = LocalMcpRegistryFactory(isolated.root / "registries")
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    with ExitStack() as stack:
        registry_local = stack.enter_context(
            registry_factory.start(
                {
                    "name": "example/registry-local",
                    "version": "1.0.0",
                    "_apm_opencode_enabled": False,
                    "packages": [
                        {
                            "registryType": "npm",
                            "identifier": "@example/server",
                            "runtimeHint": "npx",
                            "transport": {"type": "stdio"},
                        }
                    ],
                }
            )
        )
        registry_remote = stack.enter_context(
            registry_factory.start(
                {
                    "name": "example/registry-remote",
                    "version": "1.0.0",
                    "_apm_opencode_enabled": False,
                    "remotes": [{"type": "streamable-http", "url": "https://example.com/mcp"}],
                }
            )
        )
        environment = isolated.subprocess_env()
        environment["MCP_REGISTRY_ALLOW_HTTP"] = "1"
        environment["APM_TEST_LOOPBACK_PORTS"] = ",".join(
            str(urlparse(registry.url).port) for registry in (registry_local, registry_remote)
        )
        dependencies = [
            {"name": "local", "registry": False, "transport": "stdio", "command": "echo"},
            {
                "name": "remote",
                "registry": False,
                "transport": "http",
                "url": "https://example.com/mcp",
            },
            {"name": "example/registry-local", "registry": registry_local.url},
            {"name": "example/registry-remote", "registry": registry_remote.url},
        ]
        manifest = {
            "name": "enabled-lifecycle",
            "version": "1.0.0",
            "targets": ["opencode", "vscode"],
            "dependencies": {"apm": [], "mcp": dependencies},
        }
        LockFile().write(project / "apm.lock.yaml")
        previous_other_target = None
        # Equality in Python must not hide JSON type changes, including nested values.
        states = (
            {"enabled": True},
            {"enabled": 1},
            {"enabled": True},
            {"enabled": 0},
            {"enabled": False},
            {"enabled": 0},
            {"enabled": None},
            {},
            {"enabled": "false"},
            {"enabled": [False, {"nested": 1}]},
            {"enabled": [0, {"nested": True}]},
            {"enabled": {}},
            {"enabled": []},
            {},
        )
        for index, state in enumerate(states):
            for dependency in dependencies:
                dependency.pop("enabled", None)
                dependency.update(state)
                dependency["extra"] = {"enabled": False}
            dump_yaml(manifest, project / "apm.yml")
            result = runner.run(
                ("install", "--no-policy"),
                cwd=project,
                env=environment,
                scenario_id=f"opencode-enabled-transition-{index}",
            )
            assert result.returncode == 0, result.stdout + result.stderr
            rendered = json.loads(config_path.read_text(encoding="utf-8"))
            assert rendered["theme"] == "user-theme"
            assert rendered["mcp"]["unowned"] == user_server
            expected = json.dumps(state.get("enabled", True), sort_keys=True)
            for name in ("local", "remote", "registry-local", "registry-remote"):
                assert json.dumps(rendered["mcp"][name]["enabled"], sort_keys=True) == expected
            assert rendered["mcp"]["local"]["type"] == "local"
            assert rendered["mcp"]["registry-local"]["type"] == "local"
            assert rendered["mcp"]["remote"]["type"] == "remote"
            assert rendered["mcp"]["registry-remote"]["type"] == "remote"
            other_target = (project / ".vscode" / "mcp.json").read_bytes()
            if previous_other_target is not None:
                assert other_target == previous_other_target
            previous_other_target = other_target
            assert "_apm_opencode_enabled" not in other_target.decode()
        opencode_before = config_path.read_bytes()
        before = LifecycleStateSnapshot.capture(
            project,
            targets=("opencode", "vscode"),
            config_paths=(PurePosixPath("opencode.json"), PurePosixPath(".vscode/mcp.json")),
        )
        result = runner.run(("install", "--no-policy"), cwd=project, env=environment)
        assert result.returncode == 0, result.stdout + result.stderr
        after = LifecycleStateSnapshot.capture(
            project,
            targets=("opencode", "vscode"),
            config_paths=(PurePosixPath("opencode.json"), PurePosixPath(".vscode/mcp.json")),
        )
        assert after.semantic_bytes == before.semantic_bytes
        assert config_path.read_bytes() == opencode_before
        assert registry_local.request_paths
        assert registry_remote.request_paths

        project_before = config_path.read_bytes()
        user_config = isolated.home / "opencode.json"
        user_config.write_text('{"user-owned": true}\n', encoding="utf-8")
        global_manifest = {
            "name": "global-enabled",
            "version": "1.0.0",
            "targets": ["opencode"],
            "dependencies": {"apm": [], "mcp": [dependencies[0]]},
        }
        dump_yaml(global_manifest, isolated.config_root / "apm.yml")
        for _ in range(2):
            result = runner.run(
                ("install", "--global", "--runtime", "opencode", "--no-policy"),
                cwd=project,
                env=environment,
                scenario_id="opencode-global-unsupported",
            )
            assert result.returncode == 1, result.stdout + result.stderr
            assert "Skipped workspace-only runtimes at user scope: opencode" in " ".join(
                result.stdout.split()
            )
            assert config_path.read_bytes() == project_before
            assert user_config.read_text(encoding="utf-8") == '{"user-owned": true}\n'
            assert not (isolated.config_root / "opencode.json").exists()
        global_manifest["targets"] = ["codex"]
        previous_global = None
        for state in ({"enabled": False}, {"enabled": None}, {}):
            dependencies[0].pop("enabled", None)
            dependencies[0].update(state)
            dump_yaml(global_manifest, isolated.config_root / "apm.yml")
            result = runner.run(
                ("install", "--global", "--runtime", "codex", "--no-policy"),
                cwd=project,
                env=environment,
                scenario_id="opencode-global-target-isolation",
            )
            assert result.returncode == 0, result.stdout + result.stderr
            global_config = (isolated.home / ".codex" / "config.toml").read_bytes()
            if previous_global is not None:
                assert global_config == previous_global
            previous_global = global_config
            assert "enabled" not in tomlkit.parse(global_config.decode())["mcp_servers"]["local"]
            assert config_path.read_bytes() == project_before
            assert user_config.read_text(encoding="utf-8") == '{"user-owned": true}\n'

        other_project = isolated.work_root / "without-opencode"
        other_project.mkdir()
        manifest["targets"] = ["vscode"]
        manifest["dependencies"]["mcp"] = [dependencies[0]]
        dependencies[0]["enabled"] = False
        dump_yaml(manifest, other_project / "apm.yml")
        LockFile().write(other_project / "apm.lock.yaml")
        result = runner.run(
            ("install", "--no-policy"),
            cwd=other_project,
            env=environment,
            scenario_id="opencode-no-opt-in",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (other_project / ".vscode" / "mcp.json").exists()
        assert not (other_project / "opencode.json").exists()
        assert not (other_project / ".opencode").exists()
