"""Claude plugin ``${CLAUDE_PLUGIN_ROOT}`` MCP/LSP state survives a moved clone (#3825).

Releases before the fix recorded the placeholder expanded to the absolute
``apm_modules`` path, in ``apm.lock.yaml`` and in the committed native files.
A teammate's clone at another path then failed ``apm install --frozen`` and
``apm audit --ci``, and an ordinary install rewrote committed files.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.integration._path_corner_world_kit import (
    CornerWorld,
    remote_url,
    tree_bytes,
    write,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_ROOT = "${CLAUDE_PLUGIN_ROOT}"
_COMMITTED = (".git", "apm_modules")


def _claude_plugin(root: Path, name: str) -> Path:
    write(
        root / ".claude-plugin/plugin.json",
        json.dumps({"name": name, "version": "1.0.0", "description": "plugin root fixture"}),
    )
    write(
        root / ".mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    f"{name}-db": {
                        "command": "node",
                        "args": [f"{_ROOT}/scripts/server.js"],
                        "env": {"DATA": f"{_ROOT}/data"},
                    }
                }
            }
        ),
    )
    write(
        root / ".lsp.json",
        json.dumps(
            {
                "lspServers": {
                    f"{name}-ls": {
                        "command": f"{_ROOT}/scripts/server.js",
                        "args": ["--stdio"],
                        "extensionToLanguage": {f".{name}": name},
                    }
                }
            }
        ),
    )
    write(root / "scripts/server.js", "// inert fixture\n")
    return root


def test_plugin_root_mcp_and_lsp_replay_frozen_in_a_moved_clone(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = CornerWorld.create(tmp_path, apm_binary_path)
    world.publish("synth", _claude_plugin(world.isolated.package_root / "synth", "synth"))
    _claude_plugin(world.isolated.work_root / "local-synth", "loco")
    project = world.project(
        "first clone",
        targets=("claude",),
        deps=(remote_url("synth"), {"path": "../local-synth"}),
    )
    (project / ".claude").mkdir()
    world.apm(project, "install", "--no-policy")

    lock = world.lock(project)
    assert lock["mcp_configs"]["synth-db"]["args"] == [f"{_ROOT}/scripts/server.js"]
    assert lock["mcp_configs"]["loco-db"]["env"] == {"DATA": f"{_ROOT}/data"}
    assert lock["lsp_configs"]["synth-ls"]["command"] == f"{_ROOT}/scripts/server.js"
    servers = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert servers["synth-db"]["args"] == [
        "${CLAUDE_PROJECT_DIR:-.}/apm_modules/acme/synth/scripts/server.js"
    ]
    assert servers["loco-db"]["env"] == {
        "DATA": "${CLAUDE_PROJECT_DIR:-.}/apm_modules/_local/local-synth/data"
    }
    lsp_plugin = project / ".claude/skills/apm-lsp/.claude-plugin/plugin.json"
    lsp = json.loads(lsp_plugin.read_text(encoding="utf-8"))["lspServers"]
    assert lsp["synth-ls"]["command"] == (
        "${CLAUDE_PROJECT_DIR}/apm_modules/acme/synth/scripts/server.js"
    )
    committed = tree_bytes(project, exclude=_COMMITTED)
    assert str(project) not in "".join(
        text.decode("utf-8", "replace") for text in committed.values()
    )

    moved = world.isolated.work_root / "teammate clone"
    shutil.copytree(project, moved, symlinks=True)
    shutil.rmtree(moved / "apm_modules")

    world.apm(moved, "install", "--frozen", "--no-policy")
    world.apm(moved, "audit", "--ci", "--no-policy")
    assert tree_bytes(moved, exclude=_COMMITTED) == committed

    world.apm(moved, "install", "--no-policy")
    assert tree_bytes(moved, exclude=_COMMITTED) == committed
