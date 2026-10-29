"""Real CLI lifecycle coverage for the OpenCode v2 MCP layout."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

PERSONAL_SERVER = {"type": "local", "command": ["personal-server"]}
USER_TIMEOUTS = {"startup": 5000}


def test_opencode_v2_install_reinstall_and_prune(tmp_path: Path, apm_binary_path: Path) -> None:
    """A v2 config receives native entries and keeps user-owned settings."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root / "project"
    project.mkdir()
    (project / ".opencode").mkdir()
    config_path = project / "opencode.json"
    config_path.write_text(
        json.dumps(
            {
                "theme": "user-theme",
                "mcp": {"timeout": USER_TIMEOUTS, "servers": {"personal": PERSONAL_SERVER}},
            }
        ),
        encoding="utf-8",
    )
    local = {
        "name": "local",
        "registry": False,
        "transport": "stdio",
        "command": "echo",
        "enabled": False,
    }
    remote = {
        "name": "remote",
        "registry": False,
        "transport": "http",
        "url": "https://example.com/mcp",
        "extra": {"codemode": False},
    }
    manifest = {
        "name": "opencode-v2-lifecycle",
        "version": "1.0.0",
        "targets": ["opencode"],
        "dependencies": {"apm": [], "mcp": [local, remote]},
    }
    dump_yaml(manifest, project / "apm.yml")
    LockFile().write(project / "apm.lock.yaml")
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    environment = isolated.subprocess_env()

    def install(scenario_id: str):
        return runner.run(
            ("install", "--no-policy"), cwd=project, env=environment, scenario_id=scenario_id
        )

    result = install("opencode-v2-install")
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = json.loads(config_path.read_text(encoding="utf-8"))
    mcp = rendered["mcp"]
    assert rendered["theme"] == "user-theme"
    assert mcp["timeout"] == USER_TIMEOUTS
    assert set(mcp) == {"timeout", "servers"}
    servers = mcp["servers"]
    assert servers["personal"] == PERSONAL_SERVER
    assert servers["local"]["type"] == "local"
    assert servers["local"]["command"][0] == "echo"
    assert servers["local"]["disabled"] is True
    assert servers["remote"]["type"] == "remote"
    assert servers["remote"]["url"] == "https://example.com/mcp"
    assert servers["remote"]["codemode"] is False
    assert "disabled" not in servers["remote"]
    for entry in servers.values():
        assert "enabled" not in entry

    installed = config_path.read_bytes()
    result = install("opencode-v2-reinstall")
    assert result.returncode == 0, result.stdout + result.stderr
    assert config_path.read_bytes() == installed

    manifest["dependencies"]["mcp"] = [local]
    dump_yaml(manifest, project / "apm.yml")
    result = install("opencode-v2-prune")
    assert result.returncode == 0, result.stdout + result.stderr
    servers = json.loads(config_path.read_text(encoding="utf-8"))["mcp"]["servers"]
    assert set(servers) == {"personal", "local"}

    pruned = config_path.read_bytes()
    local["enabled"] = "false"
    dump_yaml(manifest, project / "apm.yml")
    install("opencode-v2-non-boolean-enabled")
    assert config_path.read_bytes() == pruned


def test_opencode_v1_install_moves_to_v2_after_opt_in(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Adding an empty mcp.servers map makes the next install write native entries."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root / "project"
    project.mkdir()
    (project / ".opencode").mkdir()
    config_path = project / "opencode.json"
    config_path.write_text(json.dumps({"mcp": {"personal": PERSONAL_SERVER}}), encoding="utf-8")
    manifest = {
        "name": "opencode-v2-migration",
        "version": "1.0.0",
        "targets": ["opencode"],
        "dependencies": {
            "apm": [],
            "mcp": [{"name": "local", "registry": False, "transport": "stdio", "command": "echo"}],
        },
    }
    dump_yaml(manifest, project / "apm.yml")
    LockFile().write(project / "apm.lock.yaml")
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    environment = isolated.subprocess_env()

    result = runner.run(
        ("install", "--no-policy"), cwd=project, env=environment, scenario_id="opencode-v1-install"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    v1_config = json.loads(config_path.read_text(encoding="utf-8"))
    assert v1_config["mcp"]["local"]["enabled"] is True
    assert "servers" not in v1_config["mcp"]

    v1_config["mcp"]["servers"] = {}
    config_path.write_text(json.dumps(v1_config), encoding="utf-8")
    result = runner.run(
        ("install", "--no-policy"), cwd=project, env=environment, scenario_id="opencode-v2-opt-in"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    mcp = json.loads(config_path.read_text(encoding="utf-8"))["mcp"]
    assert set(mcp) == {"personal", "servers"}
    assert mcp["personal"] == PERSONAL_SERVER
    assert set(mcp["servers"]) == {"local"}
    assert "enabled" not in mcp["servers"]["local"]
