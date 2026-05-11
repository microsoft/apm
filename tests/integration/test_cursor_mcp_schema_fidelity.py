"""Schema-fidelity integration tests for the Cursor MCP adapter.

Verifies that ``apm install --target cursor`` writes ``.cursor/mcp.json``
entries that conform to Cursor's native schema:

- ``type`` must be present and set to the correct transport discriminator
  (``"stdio"`` for local/stdio servers, ``"http"`` for remote servers).
- Copilot-only fields ``tools`` and ``id`` must never appear.

Regression guard for #844.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def apm_command():
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


def _write_apm_yml(project_dir: Path, mcp_servers: list[dict]) -> None:
    config = {
        "name": "cursor-schema-fidelity-e2e",
        "version": "1.0.0",
        "dependencies": {"apm": [], "mcp": mcp_servers},
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


# Keys that must NEVER appear in Cursor's .cursor/mcp.json entries.
_COPILOT_ONLY_KEYS = {"tools", "id"}


def _assert_no_copilot_fields(server_config: dict, label: str) -> None:
    for key in _COPILOT_ONLY_KEYS:
        assert key not in server_config, (
            f"Cursor server config '{label}' must not contain Copilot-only "
            f"field '{key}'.\nFull config: {server_config!r}"
        )


@pytest.mark.integration
class TestCursorStdioSchemaFidelity:
    """Verify stdio MCP servers produce Cursor-native schema on disk."""

    def test_stdio_server_emits_type_stdio(self, tmp_path, apm_command):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".cursor").mkdir()

        _write_apm_yml(
            project_dir,
            [
                {
                    "name": "test-stdio-server",
                    "registry": False,
                    "transport": "stdio",
                    "command": "echo",
                    "args": ["--greeting", "hello"],
                    "env": {"MY_VAR": "my-value"},
                }
            ],
        )

        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["APM_NON_INTERACTIVE"] = "1"
        env["MY_VAR"] = "my-value"

        result = subprocess.run(
            [apm_command, "install", "--target", "cursor"],
            cwd=project_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"apm install failed.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        mcp_json_path = project_dir / ".cursor" / "mcp.json"
        assert mcp_json_path.exists(), ".cursor/mcp.json was not created"

        mcp_config = json.loads(mcp_json_path.read_text(encoding="utf-8"))
        servers = mcp_config.get("mcpServers", {})
        assert "test-stdio-server" in servers, (
            f"Expected 'test-stdio-server' in mcpServers, got: {list(servers)}"
        )

        server = servers["test-stdio-server"]
        assert server["type"] == "stdio", f"Expected type='stdio', got: {server.get('type')!r}"
        assert server["command"] == "echo"
        assert "args" in server
        assert "--greeting" in server["args"]
        _assert_no_copilot_fields(server, "stdio")


@pytest.mark.integration
class TestCursorHttpSchemaFidelity:
    """Verify HTTP MCP servers produce Cursor-native schema on disk."""

    def test_http_server_emits_type_http(self, tmp_path, apm_command):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".cursor").mkdir()

        _write_apm_yml(
            project_dir,
            [
                {
                    "name": "test-http-server",
                    "registry": False,
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer test-token"},
                }
            ],
        )

        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["APM_NON_INTERACTIVE"] = "1"

        result = subprocess.run(
            [apm_command, "install", "--target", "cursor"],
            cwd=project_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"apm install failed.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        mcp_json_path = project_dir / ".cursor" / "mcp.json"
        assert mcp_json_path.exists(), ".cursor/mcp.json was not created"

        mcp_config = json.loads(mcp_json_path.read_text(encoding="utf-8"))
        servers = mcp_config.get("mcpServers", {})
        assert "test-http-server" in servers, (
            f"Expected 'test-http-server' in mcpServers, got: {list(servers)}"
        )

        server = servers["test-http-server"]
        assert server["type"] == "http", f"Expected type='http', got: {server.get('type')!r}"
        assert server["url"] == "https://example.com/mcp"
        _assert_no_copilot_fields(server, "http")
