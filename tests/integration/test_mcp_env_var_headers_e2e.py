"""End-to-end regression guard for #944 / PR #947: bare ${VAR} env-var
references in self-defined MCP server headers must reach VS Code's mcp.json
as the runtime-resolvable ${env:VAR} placeholder (NOT a literal ${VAR}
that VS Code would treat as opaque text).

This exercises the full pipeline:
    apm.yml  ->  apm install --target vscode  ->  .vscode/mcp.json on disk

The unit tests in tests/unit/test_vscode_adapter.py cover all three syntaxes
in isolation; this test pins the integration boundary so the fix doesn't
regress when adapter wiring changes.
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


@pytest.fixture
def temp_project(tmp_path):
    project_dir = tmp_path / "mcp-env-vars-e2e"
    project_dir.mkdir()
    # Mark this as a VS Code target via .vscode/ directory presence
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


class TestMcpEnvVarHeadersVSCode:
    """#944 regression: VS Code mcp.json must contain ${env:VAR} placeholders
    for both ${VAR} and ${env:VAR} header syntaxes from apm.yml."""

    def test_self_defined_http_server_translates_both_env_var_syntaxes(
        self, temp_project, apm_command
    ):
        """Both bare ${VAR} and explicit ${env:VAR} in apm.yml headers must
        land in mcp.json as ${env:VAR} (the syntax VS Code resolves at
        server-start time)."""
        _write_apm_yml(
            temp_project,
            [
                {
                    "name": "test-http-server",
                    "registry": False,
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "headers": {
                        # Two syntaxes per PR #947's stated VS Code contract
                        "Authorization": "Bearer ${MY_BEARER_TOKEN}",
                        "X-Api-Key": "${env:MY_API_KEY}",
                    },
                }
            ],
        )

        env = os.environ.copy()
        # Provide values for any prompt-fallback path; install must NOT
        # leak these into mcp.json (vscode emits placeholders, not values).
        env["MY_BEARER_TOKEN"] = "should-not-appear-in-vscode-json"
        env["MY_API_KEY"] = "should-not-appear-in-vscode-json"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["APM_NON_INTERACTIVE"] = "1"

        result = subprocess.run(
            [apm_command, "install", "--target", "vscode"],
            cwd=temp_project,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        # Surface install output if the flow fails so debugging is fast
        assert result.returncode == 0, (
            f"apm install failed (rc={result.returncode}).\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        mcp_json = temp_project / ".vscode" / "mcp.json"
        assert mcp_json.exists(), (
            f"Expected .vscode/mcp.json to exist after install.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        config = json.loads(mcp_json.read_text(encoding="utf-8"))
        servers = config.get("servers") or {}
        # Server keys are normalized; pick the only entry
        assert len(servers) == 1, f"Expected 1 server in mcp.json, got: {list(servers.keys())}"
        server = next(iter(servers.values()))
        headers = server.get("headers") or {}

        # ${VAR} syntax MUST be translated to ${env:VAR}
        assert headers.get("Authorization") == "Bearer ${env:MY_BEARER_TOKEN}", (
            f"Bare ${{VAR}} syntax must be translated to ${{env:VAR}} for VS Code.\n"
            f"Got: {headers!r}"
        )
        # ${env:VAR} syntax MUST be preserved
        assert headers.get("X-Api-Key") == "${env:MY_API_KEY}", (
            f"${{env:VAR}} syntax must be preserved verbatim.\nGot: {headers!r}"
        )

        # CRITICAL: literal env values from the host must NOT appear in mcp.json
        # (vscode is supposed to resolve placeholders at server-start, not at install)
        full_text = mcp_json.read_text(encoding="utf-8")
        assert "should-not-appear-in-vscode-json" not in full_text, (
            "VS Code mcp.json leaked the literal env value -- placeholder "
            "translation regressed.\n"
            f"File contents:\n{full_text}"
        )
