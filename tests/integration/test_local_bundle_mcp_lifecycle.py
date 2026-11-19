"""Local plugin bundle MCP servers follow the bundle's lifecycle (#3955).

``apm install <bundle>`` wires the bundle's ``.mcp.json`` servers under a
``bundle:<owner>`` lock provenance. ``apm audit --ci`` and ``apm install
--frozen`` must accept them as recorded baseline, and an unrelated
dependency install or uninstall must keep them, like the bundle's files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration._marketplace_pack_kit import (
    MarketplacePackWorld,
    output,
    skill_md,
    write,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_SERVER = "bundle-server"


def _mcp_package(root: Path, name: str, version: str = "1.0.0", *, mcp: bool = True) -> Path:
    manifest = (
        f"name: {name}\nversion: {version}\ndescription: m\nlicense: MIT\ntargets: [claude]\n"
        "dependencies:\n  apm: []\n"
    )
    if mcp:
        manifest += (
            f"  mcp:\n    - name: {_SERVER}\n      registry: false\n      transport: stdio\n"
            '      command: npx\n      args: ["y"]\n'
        )
    write(root / "apm.yml", manifest)
    write(root / ".apm" / "skills" / "ms" / "SKILL.md", skill_md("ms"))
    (root / ".claude").mkdir(exist_ok=True)
    return root


def _skill_package(root: Path, name: str) -> Path:
    write(root / "apm.yml", f"name: {name}\nversion: 1.0.0\ndescription: o\nlicense: MIT\n")
    write(root / ".apm" / "skills" / name / "SKILL.md", skill_md(name))
    return root


def _pack(world: MarketplacePackWorld, producer: Path, out: Path, version: str) -> Path:
    world.apm(producer, "install", "--no-policy")
    world.apm(producer, "pack", "-o", str(out))
    return out / f"{producer.name}-{version}"


def _servers(project: Path) -> list[str]:
    path = project / ".mcp.json"
    if not path.exists():
        return []
    return sorted(json.loads(path.read_text()).get("mcpServers", {}))


def test_bundle_mcp_is_recorded_baseline_and_survives_unrelated_installs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    producer = _mcp_package(world.isolated.work_root / "mcppkg", "mcppkg")
    bundle = _pack(world, producer, tmp_path / "out", "1.0.0")
    consumer = world.consumer("bundle-consumer", targets=("claude",))

    text = output(world.apm(consumer, "install", str(bundle), "--no-policy"))
    assert "Wired 1 MCP server(s) from bundle .mcp.json" in text, text
    assert _servers(consumer) == [_SERVER]
    assert world.lock(consumer)["mcp_config_provenance"] == {_SERVER: "bundle:mcppkg"}

    world.apm(consumer, "audit", "--ci", "--no-policy")
    world.apm(consumer, "install", "--frozen", "--no-policy")
    assert _servers(consumer) == [_SERVER]

    # An unrelated dependency install and uninstall keep the bundle server.
    other = _skill_package(world.isolated.work_root / "otherpkg", "otherpkg")
    text = output(world.apm(consumer, "install", str(other), "--no-policy"))
    assert "Removed stale MCP server" not in text, text
    assert _servers(consumer) == [_SERVER]
    assert (consumer / ".claude" / "skills" / "ms" / "SKILL.md").is_file()
    world.apm(consumer, "audit", "--ci", "--no-policy")
    world.apm(consumer, "install", "--frozen", "--no-policy")
    world.apm(consumer, "install", "--no-policy")
    assert _servers(consumer) == [_SERVER]
    world.apm(consumer, "uninstall", str(other))
    assert _servers(consumer) == [_SERVER]
    assert world.lock(consumer)["mcp_config_provenance"] == {_SERVER: "bundle:mcppkg"}
    world.apm(consumer, "audit", "--ci", "--no-policy")
    world.apm(consumer, "install", "--frozen", "--no-policy")
