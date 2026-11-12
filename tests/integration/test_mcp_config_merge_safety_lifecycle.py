"""Installed-CLI contract: MCP config merges keep user config files intact.

A user's Copilot ``.github/mcp.json`` with a trailing comma or VS Code
``.vscode/mcp.json`` with a JSONC comment is never rewritten: install keeps
the bytes, reports the server as failed with guidance, and converges once
the user fixes the file. A linked Claude ``.mcp.json`` is replaced by the
shared atomic writer rather than followed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.integration._fault_injection_kit import build_world, evidence

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]

_COPILOT = '{\n  "mcpServers": {\n    "user-srv": {"type": "stdio", "command": "mine"},\n  }\n}\n'
_VSCODE = (
    '{\n  // team servers\n  "servers": {"user-srv": {"type": "stdio", "command": "mine"}}\n}\n'
)


def test_unparsable_user_mcp_configs_are_kept_until_fixed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(
        tmp_path,
        apm_binary_path,
        targets=("copilot", "vscode"),
        mcp_package="alpha",
        installed=False,
    )
    copilot = world.project / ".github/mcp.json"
    vscode = world.project / ".vscode/mcp.json"
    copilot.write_text(_COPILOT, encoding="ascii")
    vscode.parent.mkdir()
    vscode.write_text(_VSCODE, encoding="ascii")

    result = world.install()

    assert result.returncode != 0, evidence(result)
    output = " ".join((result.stdout + result.stderr).split())
    assert "fix the file" in output, output
    assert copilot.read_text(encoding="ascii") == _COPILOT
    assert vscode.read_text(encoding="ascii") == _VSCODE

    copilot.write_text(_COPILOT.replace("},\n  }", "}\n  }"), encoding="ascii")
    vscode.write_text(_VSCODE.replace("  // team servers\n", ""), encoding="ascii")
    fixed = world.install()

    assert fixed.returncode == 0, evidence(fixed)
    for path, key in ((copilot, "mcpServers"), (vscode, "servers")):
        servers = json.loads(path.read_text(encoding="utf-8"))[key]
        assert {"user-srv", "alpha-srv"} <= servers.keys(), (path, servers)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_linked_claude_project_config_is_not_followed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(
        tmp_path, apm_binary_path, targets=("claude",), mcp_package="alpha", installed=False
    )
    linked = world.isolated.work_root / "linked.txt"
    linked.write_text("unrelated content\n", encoding="ascii")
    config = world.project / ".mcp.json"
    config.symlink_to(linked)

    result = world.install()

    assert result.returncode == 0, evidence(result)
    assert linked.read_text(encoding="ascii") == "unrelated content\n"
    assert not config.is_symlink()
    assert "alpha-srv" in json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
