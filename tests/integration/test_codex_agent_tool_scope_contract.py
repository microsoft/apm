"""Real-binary contract for Codex agent tool-scope loss diagnostics."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import tomllib

from apm_cli.utils.yaml_io import dump_yaml, load_yaml, yaml_to_str
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_MCP_DEPENDENCIES = [
    {
        "name": "allowed-demo",
        "registry": False,
        "transport": "stdio",
        "command": "echo",
        "args": ["allowed"],
    },
    {
        "name": "unlisted-demo",
        "registry": False,
        "transport": "stdio",
        "command": "echo",
        "args": ["unlisted"],
    },
]


def _install_codex_agent(
    root: Path,
    apm_binary_path: Path,
    *,
    name: str,
    tools: list[str] | None,
    metadata: dict[str, str] | None = None,
) -> tuple[CommandResult, Path]:
    """Author and install one Codex project through the packaged CLI boundary."""
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    project = factory.create(f"codex-{name}", targets=("codex",))
    manifest = load_yaml(project.manifest_path)
    manifest["dependencies"] = {"apm": [], "mcp": _MCP_DEPENDENCIES}
    dump_yaml(manifest, project.manifest_path)

    frontmatter = [
        "---",
        f"name: {name}",
        f"description: Codex agent {name}",
    ]
    if tools is not None:
        frontmatter.append("tools: [read, search, 'allowed-demo/*']")
    if metadata:
        frontmatter.append(yaml_to_str(metadata).rstrip("\n"))
    frontmatter.extend(("---", "", "Review the requested change.", ""))
    factory.add_agent(project, name, "\n".join(frontmatter))

    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--target", "codex"),
        scenario_id=f"codex-agent-tool-scope-{name}",
        cwd=project.root,
        env=isolated.subprocess_env(),
    )
    return result, project.root


def _read_toml(path: Path) -> dict[str, object]:
    """Read one emitted TOML artifact."""
    with path.open("rb") as handle:
        return tomllib.load(handle)


def test_codex_agent_tool_scope_is_never_silently_lost(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A scoped agent warns; its otherwise-identical no-tools twin does not."""
    scoped, scoped_root = _install_codex_agent(
        tmp_path / "scoped",
        apm_binary_path,
        name="scoped-reviewer",
        tools=["read", "search", "allowed-demo/*"],
    )
    assert scoped.returncode == 0, scoped.stdout + scoped.stderr
    scoped_agent = _read_toml(scoped_root / ".codex" / "agents" / "scoped-reviewer.toml")
    project_mcp = _read_toml(scoped_root / ".codex" / "config.toml")["mcp_servers"]
    scoped_output = scoped.stdout + scoped.stderr
    scoped_normalized = " ".join(scoped_output.split())

    assert set(project_mcp) == {"allowed-demo", "unlisted-demo"}
    assert "tools" not in scoped_agent
    assert "mcp_servers" not in scoped_agent
    assert "[!]" in scoped_output
    assert "1 lossy agent compilation warning" in scoped_normalized
    assert "scoped-reviewer.agent.md" in scoped_normalized
    assert "frontmatter field 'tools' was dropped" in scoped_normalized
    assert "may inherit all project/session MCP servers" in scoped_normalized
    assert "Fix: remove 'tools'" in scoped_normalized

    unscoped, unscoped_root = _install_codex_agent(
        tmp_path / "unscoped",
        apm_binary_path,
        name="plain-reviewer",
        tools=None,
    )
    assert unscoped.returncode == 0, unscoped.stdout + unscoped.stderr
    unscoped_agent = _read_toml(unscoped_root / ".codex" / "agents" / "plain-reviewer.toml")
    unscoped_output = unscoped.stdout + unscoped.stderr

    assert unscoped_agent["name"] == "plain-reviewer"
    assert "tools" not in unscoped_agent
    assert "mcp_servers" not in unscoped_agent
    assert "model" not in unscoped_agent
    assert "model_reasoning_effort" not in unscoped_agent
    assert "[!]" not in unscoped_output
    assert "lossy agent compilation" not in unscoped_output
    assert "frontmatter field 'tools' was dropped" not in " ".join(unscoped_output.split())


def test_codex_agent_native_models_and_dropped_metadata_reach_cli_output(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Install preserves model settings and reports APM's remaining translation loss."""
    result, project = _install_codex_agent(
        tmp_path / "native-model",
        apm_binary_path,
        name="model-reviewer",
        tools=None,
        metadata={
            "model": "gpt-5.6-sol",
            "model_reasoning_effort": "high",
            "model_verbosity": "low",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _read_toml(project / ".codex" / "agents" / "model-reviewer.toml") == {
        "name": "model-reviewer",
        "description": "Codex agent model-reviewer",
        "developer_instructions": "Review the requested change.",
        "model": "gpt-5.6-sol",
        "model_reasoning_effort": "high",
    }
    output = " ".join((result.stdout + result.stderr).split())
    assert "[!]" in output
    assert "1 lossy agent compilation warning" in output
    assert "model-reviewer.agent.md" in output
    assert "field 'model_verbosity' was dropped" in output
    assert "not translated by APM for Codex" in output
    assert "otherwise do not rely on" in output
    assert "field 'tools' was dropped" not in output
