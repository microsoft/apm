"""Local-path Claude plugin MCP servers follow the git-sourced lifecycle (#3498).

A Claude plugin (``plugin.json`` plus ``.mcp.json`` or inline ``mcpServers``)
has no authored ``apm.yml``. APM synthesizes one under ``apm_modules/_local``;
MCP collection must read it so the plugin's servers reach the selected targets
exactly as they do when the identical plugin is installed from git.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
import tomlkit
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockFile
from apm_cli.deps.plugin_parser import normalize_plugin_directory
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.policy import install_preflight as install_preflight_module
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.schema import ApmPolicy, McpPolicy
from tests.utils.materialized_downloader import stub_materialized_downloader

_SERVERS = {"demo": {"type": "http", "url": "https://example.com/mcp"}}
_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"
_PATCH_DOWNLOADER = "apm_cli.deps.github_downloader.GitHubPackageDownloader"
_PATCH_DISCOVER_GATE = "apm_cli.policy.discovery.discover_policy_with_chain"
_PATCH_DISCOVER_PREFLIGHT = f"{install_preflight_module.__name__}.discover_policy_with_chain"


def _write_plugin(directory: Path, shape: str) -> None:
    """Write the issue's plugin fixture with the requested MCP declaration shape."""
    skill = directory / "skills" / "hello"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: hello\ndescription: test\n---\nhi\n", encoding="utf-8"
    )
    manifest: dict[str, object] = {"name": "plug", "version": "0.1.0", "description": "t"}
    if shape == "dot-mcp-json":
        (directory / ".mcp.json").write_text(json.dumps({"mcpServers": _SERVERS}), encoding="utf-8")
    elif shape == "inline":
        manifest["mcpServers"] = _SERVERS
    else:
        manifest["mcpServers"] = "./servers.json"
        (directory / "servers.json").write_text(
            json.dumps({"mcpServers": _SERVERS}), encoding="utf-8"
        )
    (directory / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")


def _write_consumer(project: Path, dependency: str) -> None:
    """Write a consumer manifest selecting Claude and Codex."""
    project.mkdir(parents=True, exist_ok=True)
    # Pre-existing user Claude settings keep the project's Claude surface alive
    # independently of the plugin's own deployed files.
    (project / ".claude").mkdir(exist_ok=True)
    (project / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "consumer",
                "version": "0.0.0",
                "targets": ["claude", "codex"],
                "dependencies": {"apm": [dependency]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _install(project: Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    monkeypatch.chdir(project)
    clear_apm_yml_cache()
    return CliRunner().invoke(cli, ["install", *args])


def _native_state(project: Path) -> tuple[dict, dict, dict]:
    """Return Claude servers, Codex servers, and lockfile MCP ownership."""
    claude = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    codex = tomlkit.parse((project / ".codex" / "config.toml").read_text(encoding="utf-8")).unwrap()
    lock = LockFile.read(project / "apm.lock.yaml")
    assert lock is not None
    ownership = {
        "servers": lock.mcp_servers,
        "configs": lock.mcp_configs,
        "targets": lock.mcp_target_servers,
    }
    return claude, codex["mcp_servers"], ownership


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


@pytest.mark.parametrize("shape", ["dot-mcp-json", "inline", "path"])
@patch(_PATCH_UPDATES, return_value=None)
@patch(_PATCH_DOWNLOADER)
def test_local_plugin_mcp_matches_git_sourced_plugin(
    mock_downloader, _updates, workspace: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """The local plugin writes the same native MCP state as its git twin."""
    _write_plugin(workspace / "plug", shape)

    git_project = workspace / "git-consumer"
    _write_consumer(git_project, "acme/plug")
    git_source = git_project / "apm_modules" / "acme" / "plug"
    shutil.copytree(workspace / "plug", git_source)
    normalize_plugin_directory(git_source, git_source / "plugin.json")
    stub_materialized_downloader(mock_downloader, git_project / "apm_modules")
    git_result = _install(git_project, monkeypatch, "--no-policy")
    assert git_result.exit_code == 0, git_result.output

    local_project = workspace / "consumer"
    _write_consumer(local_project, "../plug")
    local_result = _install(local_project, monkeypatch, "--no-policy")
    assert local_result.exit_code == 0, local_result.output
    assert not (workspace / "plug" / "apm.yml").exists()

    claude, codex, ownership = _native_state(local_project)
    assert claude == _SERVERS
    assert codex == {"demo": {"url": "https://example.com/mcp"}}
    assert ownership["targets"] == {"claude": ["demo"], "codex": ["demo"]}
    assert (claude, codex, ownership) == _native_state(git_project)
    lock = LockFile.read(local_project / "apm.lock.yaml")
    assert lock is not None
    assert lock.mcp_config_provenance == {"demo": "plug"}


@patch(_PATCH_UPDATES, return_value=None)
def test_local_plugin_mcp_is_replayed_audited_and_uninstalled(
    _updates, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Frozen replay, audit consistency, and uninstall cleanup own the server."""
    _write_plugin(workspace / "plug", "dot-mcp-json")
    project = workspace / "consumer"
    _write_consumer(project, "../plug")
    assert _install(project, monkeypatch, "--no-policy").exit_code == 0
    locked = (project / "apm.lock.yaml").read_bytes()

    frozen = _install(project, monkeypatch, "--frozen", "--no-policy")
    assert frozen.exit_code == 0, frozen.output
    audit = CliRunner().invoke(cli, ["audit", "--ci", "--no-policy"])
    assert audit.exit_code == 0, audit.output
    assert (project / "apm.lock.yaml").read_bytes() == locked

    removed = CliRunner().invoke(cli, ["uninstall", "../plug"])
    assert removed.exit_code == 0, removed.output
    claude = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert "demo" not in claude.get("mcpServers", {})
    codex = tomlkit.parse((project / ".codex" / "config.toml").read_text(encoding="utf-8")).unwrap()
    assert "demo" not in codex.get("mcp_servers", {})


@patch(_PATCH_UPDATES, return_value=None)
@patch(_PATCH_DISCOVER_PREFLIGHT)
@patch(_PATCH_DISCOVER_GATE)
def test_local_plugin_mcp_is_subject_to_policy_preflight(
    mock_gate, mock_preflight, _updates, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocking MCP policy refuses the local plugin's server before any write."""
    fetch = PolicyFetchResult(
        policy=ApmPolicy(enforcement="block", mcp=McpPolicy(deny=("demo",))),
        source="org:test-org/.github",
        outcome="found",
    )
    mock_gate.return_value = fetch
    mock_preflight.return_value = fetch
    _write_plugin(workspace / "plug", "dot-mcp-json")
    project = workspace / "consumer"
    _write_consumer(project, "../plug")

    result = _install(project, monkeypatch)

    assert result.exit_code != 0, result.output
    assert "demo" in result.output
    assert not (project / ".mcp.json").exists()
    assert not (project / ".codex" / "config.toml").exists()
