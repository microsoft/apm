"""Installed-CLI contract: Claude plugin .mcp.json in flat and wrapped shapes (#3499)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import tomlkit

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.requires_apm_binary]

_REMOTE = "https://github.com/apm-fixture-org/linear-plugin.git"
_SERVERS = {
    "linear": {"type": "http", "url": "https://mcp.linear.example.invalid/mcp"},
    "linear-local": {"command": "inert-fixture-command", "args": ["--readonly"]},
}


def _install(tmp_path: Path, apm_binary_path: Path, shape: str) -> tuple[Path, str]:
    """Install a pinned Claude plugin whose .mcp.json uses *shape*."""
    isolated = IsolatedApmEnvironment.create(tmp_path / shape, base_env=dict(os.environ))
    env = isolated.subprocess_env()
    source = isolated.package_root / "linear-plugin"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "linear", "version": "1.0.0", "description": "Inert fixture"}),
        encoding="utf-8",
    )
    payload = {
        "flat": _SERVERS,
        "wrapped": {"mcpServers": _SERVERS},
        "unusable": {"linear": "https://mcp.linear.example.invalid/mcp"},
        "malformed": "{not json",
    }[shape]
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (source / ".mcp.json").write_text(text, encoding="utf-8")
    skill = source / "skills/linear-basics"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: linear-basics\ndescription: Inert fixture\n---\n# Fixture\n",
        encoding="utf-8",
    )
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repository = repositories.create("linear-plugin", source_tree=source)
    pin = repositories.commit(repository, message="pinned flat-shape plugin fixture")
    env = repositories.url_rewrite_subprocess_env(repository, _REMOTE)

    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "dependencies": {"apm": [{"git": _REMOTE, "ref": pin.sha}]},
        },
        project / "apm.yml",
    )
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--target", "claude,codex", "--no-policy", "--parallel-downloads", "0"),
        cwd=project,
        env=env,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    return project, output


@pytest.mark.parametrize("shape", ["flat", "wrapped"])
def test_plugin_mcp_json_shape_reaches_claude_and_codex(
    tmp_path: Path, apm_binary_path: Path, shape: str
) -> None:
    project, output = _install(tmp_path, apm_binary_path, shape)

    synthesized = next((project / "apm_modules").rglob("linear-plugin/apm.yml"))
    mcp = {entry["name"]: entry for entry in load_yaml(synthesized)["dependencies"]["mcp"]}
    assert set(mcp) == {"linear", "linear-local"}
    assert mcp["linear"]["url"] == _SERVERS["linear"]["url"]
    assert mcp["linear-local"]["command"] == "inert-fixture-command"

    claude = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert claude["linear"]["url"] == _SERVERS["linear"]["url"]
    assert claude["linear-local"]["command"] == "inert-fixture-command"
    assert claude["linear-local"]["args"] == ["--readonly"]

    codex = tomlkit.parse((project / ".codex/config.toml").read_text(encoding="utf-8"))
    codex_servers = codex["mcp_servers"]
    assert codex_servers["linear"]["url"] == _SERVERS["linear"]["url"]
    assert codex_servers["linear-local"]["command"] == "inert-fixture-command"
    assert list(codex_servers["linear-local"]["args"]) == ["--readonly"]

    assert set(load_yaml(project / "apm.lock.yaml")["mcp_servers"]) == {"linear", "linear-local"}
    assert "declares no usable MCP servers" not in output


@pytest.mark.parametrize("shape", ["unusable", "malformed"])
def test_plugin_mcp_json_without_usable_servers_warns_by_name(
    tmp_path: Path, apm_binary_path: Path, shape: str
) -> None:
    project, output = _install(tmp_path, apm_binary_path, shape)

    synthesized = next((project / "apm_modules").rglob("linear-plugin/apm.yml"))
    assert not load_yaml(synthesized).get("dependencies", {}).get("mcp")
    assert not load_yaml(project / "apm.lock.yaml").get("mcp_servers")
    flattened = " ".join(output.split())
    assert "MCP config '.mcp.json' in plugin 'linear' declares no usable MCP servers" in (flattened)
    assert "mcp.linear.example.invalid" not in output
    assert flattened.count("declares no usable MCP servers") == 1
    assert str(tmp_path) not in output
