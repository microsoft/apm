"""Removing a slashed MCP server keeps short-name entries APM did not write (#3493, #3649).

A server named ``com.example/server`` must be removed from every native MCP
config by its full name only.  A user-authored ``server`` entry, or a
declared plain ``server`` sharing the final path segment, survives the
removal and ``apm audit --ci`` stays clean.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration._mcp_chain_kit import JSON_CONFIGS, Chain, stdio_server

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]


def test_removing_slashed_server_keeps_user_and_declared_short_name_entries(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3493: a short-name entry APM did not write is never removed for a slashed server."""
    chain = Chain(
        tmp_path / "slashed",
        apm_binary_path,
        dirs=(".claude", ".vscode", ".cursor", ".github", ".codex", ".opencode"),
    )
    user_entries = {
        "claude": {"command": "user-own"},
        "vscode": {"type": "stdio", "command": "user-own"},
        "cursor": {"command": "user-own"},
        "copilot": {"type": "local", "command": "user-own", "tools": ["*"]},
        "opencode": {"type": "local", "command": ["user-own"]},
    }
    for target, entry in user_entries.items():
        relative, key = JSON_CONFIGS[target]
        (chain.project / relative).write_text(json.dumps({key: {"server": entry}}), "utf-8")
    targets = "claude,vscode,cursor,copilot,opencode"

    chain.write_manifest([stdio_server("com.example/server", "slashed")])
    chain.ok("install", "--no-policy", "--target", targets)
    for target, entry in user_entries.items():
        assert chain.servers(target)["server"] == entry, target
        assert "com.example/server" in chain.servers(target), target

    chain.write_manifest([])
    chain.ok("install", "--no-policy", "--target", targets)
    for target, entry in user_entries.items():
        assert chain.servers(target) == {"server": entry}, target

    # A declared plain server sharing the final segment survives the slashed removal.
    for target in user_entries:
        relative, key = JSON_CONFIGS[target]
        (chain.project / relative).write_text(json.dumps({key: {}}), "utf-8")
    chain.write_manifest(
        [stdio_server("com.example/server", "slashed"), stdio_server("server", "plain")]
    )
    chain.ok("install", "--no-policy", "--target", targets)
    chain.write_manifest([stdio_server("server", "plain")])
    chain.ok("install", "--no-policy", "--target", targets)
    for target in user_entries:
        assert set(chain.servers(target)) == {"server"}, target
    chain.ok("audit", "--ci", "--no-policy")
