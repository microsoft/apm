"""End-to-end regression guard for #1152: Copilot CLI's mcp-config.json
must contain ``${VAR}`` runtime placeholders for env-var references in
apm.yml -- never the literal value resolved at install time.

This exercises the full pipeline:
    apm.yml  ->  apm install --target copilot  ->  ~/.copilot/mcp-config.json

The unit tests in tests/unit/test_copilot_adapter.py cover translation in
isolation; this test pins the integration boundary so plaintext secrets
cannot regress back onto disk.

Also includes a Cursor regression trap: Cursor's adapter is pinned to
the legacy install-time resolution behaviour (per the design contract
in copilot.py) until its config format is individually audited. That
adapter MUST keep producing literal values; this test fails loudly if
the Copilot translation accidentally bleeds into Cursor.
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


def _write_apm_yml(project_dir, mcp_servers):
    config = {
        "name": "mcp-env-vars-copilot-e2e",
        "version": "1.0.0",
        "dependencies": {"apm": [], "mcp": mcp_servers},
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


class TestMcpEnvVarHeadersCopilot:
    """#1152 regression: Copilot mcp-config.json must contain ``${VAR}``
    runtime placeholders for env-var references in apm.yml. The literal
    values from the installer's environment must NEVER appear on disk.
    """

    def test_self_defined_http_server_translates_env_vars_not_resolves(self, tmp_path, apm_command):
        """``${VAR}`` and ``${env:VAR}`` syntaxes in apm.yml headers must
        land in mcp-config.json as ``${VAR}`` (Copilot CLI's native runtime
        substitution syntax). No host env values may leak into the file.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        # Copilot target signal: presence of .github/ activates the
        # copilot/vscode runtime detection chain.
        (project_dir / ".github").mkdir()

        # Isolated HOME so we don't touch the developer's real
        # ~/.copilot/mcp-config.json. The Copilot adapter uses
        # ``Path.home() / ".copilot"``.
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        _write_apm_yml(
            project_dir,
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
        env["HOME"] = str(fake_home)
        # Sentinel values: if these strings ever appear in the rendered
        # config, the security contract has regressed.
        env["MY_BEARER_TOKEN"] = "should-not-appear-in-copilot-json"
        env["MY_API_KEY"] = "should-not-appear-in-copilot-json"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["APM_NON_INTERACTIVE"] = "1"

        result = subprocess.run(
            [apm_command, "install", "--target", "copilot"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, (
            f"apm install failed (rc={result.returncode}).\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        mcp_config = fake_home / ".copilot" / "mcp-config.json"
        assert mcp_config.exists(), (
            f"Expected ~/.copilot/mcp-config.json to exist after install.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        config = json.loads(mcp_config.read_text(encoding="utf-8"))
        servers = config.get("mcpServers") or {}
        assert len(servers) == 1, (
            f"Expected 1 server in mcp-config.json, got: {list(servers.keys())}"
        )
        server = next(iter(servers.values()))
        headers = server.get("headers") or {}

        # ``${VAR}`` already Copilot-native: pass through unchanged.
        assert headers.get("Authorization") == "Bearer ${MY_BEARER_TOKEN}", (
            f"Bare ${{VAR}} must remain ${{VAR}} for Copilot CLI.\nGot: {headers!r}"
        )
        # ``${env:VAR}`` translated to ``${VAR}`` (env: prefix stripped).
        assert headers.get("X-Api-Key") == "${MY_API_KEY}", (
            f"${{env:VAR}} must translate to ${{VAR}} for Copilot CLI.\nGot: {headers!r}"
        )

        # CRITICAL: no plaintext secret may appear anywhere in the file.
        full_text = mcp_config.read_text(encoding="utf-8")
        assert "should-not-appear-in-copilot-json" not in full_text, (
            "Copilot mcp-config.json leaked the literal env value -- "
            "the install-time translation regressed and secrets are now "
            "baked to disk.\n"
            f"File contents:\n{full_text}"
        )

    def test_self_defined_stdio_server_translates_env_vars_in_args(self, tmp_path, apm_command):
        """Self-defined stdio server with env-var placeholders in BOTH the
        ``env`` block and ``args`` list must land in mcp-config.json with
        ``${VAR}`` runtime placeholders. Closes the integration-tier gap
        flagged by test-coverage review for the ``_raw_stdio`` branch of
        ``_format_server_config`` and the supply-chain regression where
        the dict-shaped ``env`` block was silently dropped to ``{}``.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".github").mkdir()
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        _write_apm_yml(
            project_dir,
            [
                {
                    "name": "test-stdio-server",
                    "registry": False,
                    "transport": "stdio",
                    "command": "echo",
                    "args": [
                        "--token=${env:MY_STDIO_TOKEN}",
                        "--bearer=${MY_STDIO_TOKEN}",
                        "--legacy=<MY_LEGACY_VAR>",
                    ],
                    "env": {
                        "PRIMARY_TOKEN": "${MY_STDIO_TOKEN}",
                        "PREFIXED_TOKEN": "${env:MY_STDIO_TOKEN}",
                        "LEGACY_TOKEN": "<MY_LEGACY_VAR>",
                    },
                }
            ],
        )

        env = os.environ.copy()
        env["HOME"] = str(fake_home)
        env["MY_STDIO_TOKEN"] = "stdio-secret-must-not-leak"
        env["MY_LEGACY_VAR"] = "legacy-secret-must-not-leak"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["APM_NON_INTERACTIVE"] = "1"

        result = subprocess.run(
            [apm_command, "install", "--target", "copilot"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, (
            f"apm install failed (rc={result.returncode}).\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        mcp_config = fake_home / ".copilot" / "mcp-config.json"
        assert mcp_config.exists(), (
            f"Expected ~/.copilot/mcp-config.json to exist after install.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        config = json.loads(mcp_config.read_text(encoding="utf-8"))
        servers = config.get("mcpServers") or {}
        assert len(servers) == 1, (
            f"Expected 1 server in mcp-config.json, got: {list(servers.keys())}"
        )
        server = next(iter(servers.values()))

        # env block: all three syntaxes translate to ${VAR}.
        env_block = server.get("env") or {}
        assert env_block.get("PRIMARY_TOKEN") == "${MY_STDIO_TOKEN}", (
            f"Bare ${{VAR}} in stdio env must remain ${{VAR}}.\nGot: {env_block!r}"
        )
        assert env_block.get("PREFIXED_TOKEN") == "${MY_STDIO_TOKEN}", (
            f"${{env:VAR}} in stdio env must translate to ${{VAR}}.\nGot: {env_block!r}"
        )
        assert env_block.get("LEGACY_TOKEN") == "${MY_LEGACY_VAR}", (
            f"Legacy <VAR> in stdio env must translate to ${{VAR}}.\nGot: {env_block!r}"
        )

        # args list: all three syntaxes translate to ${VAR}.
        args = server.get("args") or []
        assert "--token=${MY_STDIO_TOKEN}" in args, (
            f"${{env:VAR}} in stdio args must translate to ${{VAR}}.\nGot: {args!r}"
        )
        assert "--bearer=${MY_STDIO_TOKEN}" in args, (
            f"Bare ${{VAR}} in stdio args must remain ${{VAR}}.\nGot: {args!r}"
        )
        assert "--legacy=${MY_LEGACY_VAR}" in args, (
            f"Legacy <VAR> in stdio args must translate to ${{VAR}}.\nGot: {args!r}"
        )

        # CRITICAL: neither secret may appear as a literal anywhere.
        full_text = mcp_config.read_text(encoding="utf-8")
        assert "stdio-secret-must-not-leak" not in full_text, (
            f"Copilot stdio config leaked MY_STDIO_TOKEN as plaintext.\nFile contents:\n{full_text}"
        )
        assert "legacy-secret-must-not-leak" not in full_text, (
            f"Copilot stdio config leaked MY_LEGACY_VAR as plaintext.\nFile contents:\n{full_text}"
        )


class TestMcpEnvVarHeadersCursor:
    """Sibling-adapter regression trap for #1152.

    Cursor's mcp.json runtime-substitution support has not yet been
    individually audited, so its adapter is pinned to the legacy
    install-time resolution behaviour. This test fails if that pin
    accidentally lifts -- either by removing the
    ``_supports_runtime_env_substitution = False`` override on
    ``CursorClientAdapter`` or by changing the base class default in
    a way that breaks Cursor.
    """

    def test_cursor_still_resolves_env_vars_to_literal(self, tmp_path, apm_command):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        # Cursor target signal.
        (project_dir / ".cursor").mkdir()

        _write_apm_yml(
            project_dir,
            [
                {
                    "name": "test-http-server",
                    "registry": False,
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "headers": {
                        "Authorization": "Bearer ${MY_BEARER_TOKEN}",
                    },
                }
            ],
        )

        env = os.environ.copy()
        env["MY_BEARER_TOKEN"] = "literal-cursor-value"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["APM_NON_INTERACTIVE"] = "1"

        result = subprocess.run(
            [apm_command, "install", "--target", "cursor"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, (
            f"apm install failed (rc={result.returncode}).\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        cursor_config = project_dir / ".cursor" / "mcp.json"
        assert cursor_config.exists(), (
            f"Expected .cursor/mcp.json to exist after install.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        config = json.loads(cursor_config.read_text(encoding="utf-8"))
        servers = config.get("mcpServers") or {}
        assert len(servers) == 1, (
            f"Expected 1 server in cursor mcp.json, got: {list(servers.keys())}"
        )
        server = next(iter(servers.values()))
        headers = server.get("headers") or {}

        # Cursor MUST keep the legacy resolve-to-literal behaviour
        # until a per-adapter audit lifts the pin. This guard fires
        # if the Copilot fix accidentally bleeds into Cursor.
        assert headers.get("Authorization") == "Bearer literal-cursor-value", (
            f"Cursor adapter unexpectedly stopped resolving env vars at "
            f"install time. If this is intentional, update the design "
            f"contract in copilot.py and remove this regression trap.\n"
            f"Got: {headers!r}"
        )
