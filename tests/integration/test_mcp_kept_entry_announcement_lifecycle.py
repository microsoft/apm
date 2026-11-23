"""A kept MCP entry is announced on every install path (#4861).

PROMISE: P-INV-I4 -- a kept entry is announced on every later install
(source: install/mcp/write_receipts.py, #4801/#4818).
CELL: OP=install SCOPE=project LOCK=present PRIM=mcp MCPOWN=claude
PATH={--mcp NAME, mcp install NAME, local bundle}.
OWNER: install/mcp/write_receipts.py (announce_kept_mcp_writes).

The kept state is reached the way users reach it: an APM-written entry is
edited by hand, its declaration leaves apm.yml and apm.lock.yaml is deleted,
so the next install keeps the entry and marks it kept. Every later install
path must say so again; nothing is removed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, write

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I4"),
]

SERVER = "kept-mcp"
_KEPT = f"Kept MCP server '{SERVER}'"


def _servers(world: ReplayWorld) -> dict:
    return json.loads((world.project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]


def _kept_world(tmp_path: Path, apm_binary_path: Path) -> ReplayWorld:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    manifest = {
        "name": "proj",
        "version": "1.0.0",
        "targets": ["claude"],
        "dependencies": {
            "mcp": [
                {
                    "name": SERVER,
                    "registry": False,
                    "transport": "stdio",
                    "command": "apm-inert-server",
                    "args": ["--stdio"],
                }
            ]
        },
    }
    dump_yaml(manifest, world.project / "apm.yml")
    world.apm("install")
    config = world.project / ".mcp.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["mcpServers"][SERVER]["args"] = ["--stdio", "--edited"]
    config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    manifest["dependencies"]["mcp"] = []
    dump_yaml(manifest, world.project / "apm.yml")
    (world.project / "apm.lock.yaml").unlink()
    kept = world.apm("install")
    assert _KEPT in kept.stdout + kept.stderr, kept.stdout + kept.stderr
    return world


@pytest.mark.parametrize(
    "argv",
    [
        ("install", "--mcp", "other-mcp", "--", "apm-inert-server", "--other"),
        ("mcp", "install", "other-mcp", "--", "apm-inert-server", "--other"),
    ],
    ids=["install-mcp", "mcp-install"],
)
def test_adding_an_mcp_server_announces_the_kept_entry(
    tmp_path: Path, apm_binary_path: Path, argv: tuple[str, ...]
) -> None:
    world = _kept_world(tmp_path, apm_binary_path)
    edited = _servers(world)[SERVER]

    result = world.apm(*argv)

    assert _KEPT in " ".join((result.stdout + result.stderr).split())
    assert _servers(world)[SERVER] == edited, "announcing must not change the kept entry"
    assert "other-mcp" in _servers(world)


def test_a_local_bundle_install_announces_the_kept_entry(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _kept_world(tmp_path, apm_binary_path)
    edited = _servers(world)[SERVER]
    bundle = tmp_path / "bundle"
    write(bundle / "plugin.json", json.dumps({"name": "notes", "version": "1.0.0"}) + "\n")
    write(
        bundle / "skills" / "notes" / "SKILL.md",
        "---\nname: notes\ndescription: Notes skill\n---\n# notes\n",
    )

    result = world.apm("install", str(bundle))

    assert _KEPT in " ".join((result.stdout + result.stderr).split())
    assert _servers(world)[SERVER] == edited, "announcing must not change the kept entry"
    assert SERVER not in (load_yaml(world.project / "apm.yml").get("dependencies") or {}).get(
        "mcp", []
    )
