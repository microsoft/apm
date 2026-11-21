"""Installed-CLI contract: MCP config merges keep user config files intact.

A user's Copilot ``.mcp.json`` with a trailing comma or Cursor
``.cursor/mcp.json`` with a JSONC comment is never rewritten: install keeps
the bytes, reports the server as failed with guidance, and converges once
the user fixes the file. Claude's ``.mcp.json`` follows the same rule, and
project MCP configs that are symbolic links are refused like user-scope ones.
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
    pytest.mark.lifecycle_smoke,
]

_COPILOT = '{\n  "mcpServers": {\n    "user-srv": {"type": "stdio", "command": "mine"},\n  }\n}\n'
_CURSOR = '{\n  // team servers\n  "mcpServers": {"user-srv": {"command": "mine"}}\n}\n'


def test_unparsable_user_mcp_configs_are_kept_until_fixed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = build_world(
        tmp_path,
        apm_binary_path,
        targets=("copilot", "cursor"),
        mcp_package="alpha",
        installed=False,
    )
    copilot = world.project / ".mcp.json"
    cursor = world.project / ".cursor/mcp.json"
    copilot.write_text(_COPILOT, encoding="ascii")
    cursor.parent.mkdir(exist_ok=True)
    cursor.write_text(_CURSOR, encoding="ascii")

    result = world.install()

    assert result.returncode != 0, evidence(result)
    output = " ".join((result.stdout + result.stderr).split())
    assert "fix the file" in output, output
    assert copilot.read_text(encoding="ascii") == _COPILOT
    assert cursor.read_text(encoding="ascii") == _CURSOR

    copilot.write_text(_COPILOT.replace("},\n  }", "}\n  }"), encoding="ascii")
    cursor.write_text(_CURSOR.replace("  // team servers\n", ""), encoding="ascii")
    fixed = world.install()

    assert fixed.returncode == 0, evidence(fixed)
    for path in (copilot, cursor):
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        assert {"user-srv", "alpha-srv"} <= servers.keys(), (path, servers)


def test_unparsable_claude_project_config_is_kept(tmp_path: Path, apm_binary_path: Path) -> None:
    world = build_world(
        tmp_path, apm_binary_path, targets=("claude",), mcp_package="alpha", installed=False
    )
    config = world.project / ".mcp.json"
    config.write_text(_COPILOT, encoding="ascii")

    result = world.install()

    assert result.returncode != 0, evidence(result)
    assert "fix the file" in " ".join((result.stdout + result.stderr).split()), evidence(result)
    assert config.read_text(encoding="ascii") == _COPILOT


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_linked_claude_project_config_is_refused(tmp_path: Path, apm_binary_path: Path) -> None:
    world = build_world(
        tmp_path, apm_binary_path, targets=("claude",), mcp_package="alpha", installed=False
    )
    linked = world.isolated.work_root / "linked.json"
    linked.write_text('{"mcpServers": {}}\n', encoding="ascii")
    config = world.project / ".mcp.json"
    config.symlink_to(linked)

    result = world.install()

    assert result.returncode != 0, evidence(result)
    output = " ".join((result.stdout + result.stderr).split())
    assert "does not write MCP config through symlinks" in output, output
    assert config.is_symlink()
    assert linked.read_text(encoding="ascii") == '{"mcpServers": {}}\n'

    config.unlink()
    fixed = world.install()
    assert fixed.returncode == 0, evidence(fixed)
    assert "alpha-srv" in json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
