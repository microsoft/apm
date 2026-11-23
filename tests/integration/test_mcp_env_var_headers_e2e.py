"""End-to-end regression guard for #944 / PR #947 under the ``vscode`` alias.

``vscode`` is a deprecated alias of ``copilot`` (#3866): self-defined MCP
header references written with ``--target vscode`` reach the workspace-root
``.mcp.json`` as runtime ``${VAR}`` placeholders -- never as the literal value
resolved at install time, and never in the retired ``.vscode/mcp.json``.

This exercises the full pipeline:
    apm.yml  ->  apm install --target vscode  ->  .mcp.json on disk
"""

import json
import os
import subprocess

import pytest
import yaml

pytestmark = pytest.mark.e2e


@pytest.fixture
def temp_project(tmp_path):
    project_dir = tmp_path / "mcp-env-vars-e2e"
    project_dir.mkdir()
    (project_dir / ".vscode").mkdir()
    return project_dir


def _write_apm_yml(project_dir, mcp_servers):
    config = {
        "name": "mcp-env-vars-e2e",
        "version": "1.0.0",
        "dependencies": {"apm": [], "mcp": mcp_servers},
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


class TestMcpEnvVarHeadersVSCodeAlias:
    """#944 regression: the vscode alias keeps header references as ${VAR}."""

    def test_self_defined_http_server_translates_both_env_var_syntaxes(
        self, temp_project, apm_binary_path
    ):
        """Bare ${VAR} and explicit ${env:VAR} both land in .mcp.json as ${VAR}."""
        _write_apm_yml(
            temp_project,
            [
                {
                    "name": "test-http-server",
                    "registry": False,
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "headers": {
                        "Authorization": "Bearer ${MY_BEARER_TOKEN}",
                        "X-Api-Key": "${env:MY_API_KEY}",
                    },
                }
            ],
        )

        env = os.environ.copy()
        # Provide values for any prompt-fallback path; install must NOT
        # leak these into the config (it writes placeholders, not values).
        env["MY_BEARER_TOKEN"] = "should-not-appear-in-mcp-json"
        env["MY_API_KEY"] = "should-not-appear-in-mcp-json"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["APM_NON_INTERACTIVE"] = "1"

        result = subprocess.run(
            [apm_binary_path, "install", "--target", "vscode"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, (
            f"apm install failed (rc={result.returncode}).\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
        assert "replace `vscode` with `copilot`" in " ".join(result.stderr.split())
        assert not (temp_project / ".vscode" / "mcp.json").exists()

        mcp_json = temp_project / ".mcp.json"
        assert mcp_json.exists(), (
            f"Expected .mcp.json to exist after install.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        config = json.loads(mcp_json.read_text(encoding="utf-8"))
        servers = config.get("mcpServers") or {}
        assert len(servers) == 1, f"Expected 1 server in .mcp.json, got: {list(servers.keys())}"
        server = next(iter(servers.values()))
        headers = server.get("headers") or {}

        assert headers.get("Authorization") == "Bearer ${MY_BEARER_TOKEN}", (
            f"Bare ${{VAR}} syntax must stay ${{VAR}}.\nGot: {headers!r}"
        )
        assert headers.get("X-Api-Key") == "${MY_API_KEY}", (
            f"${{env:VAR}} syntax must be written as ${{VAR}}.\nGot: {headers!r}"
        )

        # CRITICAL: literal env values from the host must NOT appear on disk.
        full_text = mcp_json.read_text(encoding="utf-8")
        assert "should-not-appear-in-mcp-json" not in full_text, (
            "The project .mcp.json leaked the literal env value -- placeholder "
            "translation regressed.\n"
            f"File contents:\n{full_text}"
        )
