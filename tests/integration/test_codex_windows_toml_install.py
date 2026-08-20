"""Codex install-flow regression tests for Windows TOML path keys."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import tomlkit

from apm_cli.integration.mcp_integrator import MCPIntegrator


def test_codex_install_preserves_windows_literal_paths(tmp_path: Path) -> None:
    """Install a server without rewriting unrelated Windows path settings."""
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    unrelated = (
        "[projects.'c:\\src\\projectdir\\subdir']\n"
        'trust_level = "trusted"\n'
        "\n"
        "[desktop.open-in-target-preferences.perPath]\n"
        "'C:\\Users\\me\\Documents\\Playground' = \"fileManager\"\n"
    )
    config_path.write_text(unrelated, encoding="utf-8", newline="\n")
    server_info = {
        "name": "windows-safe",
        "_raw_stdio": {
            "command": "node",
            "args": [r"C:\tools\mcp\server.js"],
            "env": {"CACHE_DIR": r"C:\Users\me\.cache\mcp"},
        },
    }

    installed = MCPIntegrator._install_for_runtime(
        "codex",
        ["windows-safe"],
        server_info_cache={"windows-safe": server_info},
        project_root=tmp_path,
        logger=MagicMock(),
    )

    updated = config_path.read_text(encoding="utf-8")
    parsed = tomlkit.parse(updated)
    assert installed is True
    assert unrelated in updated
    assert parsed["projects"][r"c:\src\projectdir\subdir"]["trust_level"] == "trusted"
    assert (
        parsed["desktop"]["open-in-target-preferences"]["perPath"][
            r"C:\Users\me\Documents\Playground"
        ]
        == "fileManager"
    )
    assert parsed["mcp_servers"]["windows-safe"]["command"] == "node"
    assert parsed["mcp_servers"]["windows-safe"]["args"] == [r"C:\tools\mcp\server.js"]
    assert parsed["mcp_servers"]["windows-safe"]["env"]["CACHE_DIR"] == (r"C:\Users\me\.cache\mcp")


def test_codex_stale_cleanup_preserves_windows_literal_paths(tmp_path: Path) -> None:
    """Clean stale servers through public runtime dispatch without data loss."""
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    unrelated = (
        "[projects.'c:\\src\\projectdir\\subdir']\n"
        'trust_level = "trusted"\n'
        "\n"
        "[desktop.open-in-target-preferences.perPath]\n"
        "'C:\\Users\\me\\Documents\\Playground' = \"fileManager\"\n"
    )
    config_path.write_text(
        unrelated
        + "\n[mcp_servers.stale-server]\n"
        + 'command = "old"\n'
        + "\n[mcp_servers.keep-server]\n"
        + 'command = "keep"\n',
        encoding="utf-8",
        newline="\n",
    )

    with patch("apm_cli.integration.mcp_integrator._rich_success"):
        MCPIntegrator.remove_stale(
            {"stale-server"},
            runtime="codex",
            project_root=tmp_path,
            logger=MagicMock(),
        )

    updated = config_path.read_text(encoding="utf-8")
    parsed = tomlkit.parse(updated)
    assert unrelated in updated
    assert "stale-server" not in parsed["mcp_servers"]
    assert parsed["mcp_servers"]["keep-server"]["command"] == "keep"
    assert parsed["projects"][r"c:\src\projectdir\subdir"]["trust_level"] == "trusted"
