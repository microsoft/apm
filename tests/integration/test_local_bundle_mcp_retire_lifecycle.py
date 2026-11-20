"""A local plugin bundle's MCP servers retire like its hooks (#3956).

A reinstall of the bundle whose MCP servers are no longer approved, are
denied, or are no longer shipped must remove the servers the bundle wired
earlier and clear their ``bundle:<owner>`` lock provenance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.bundle.local_bundle import detect_local_bundle
from apm_cli.security.executables import local_bundle_approval_key
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._marketplace_pack_kit import (
    MarketplacePackWorld,
    output,
    skill_md,
    write,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_SERVER = "bundle-server"


def _pack(world: MarketplacePackWorld, root: Path, out: Path, version: str, *, mcp: bool) -> Path:
    manifest = (
        f"name: mcppkg\nversion: {version}\ndescription: m\nlicense: MIT\ntargets: [claude]\n"
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
    world.apm(root, "install", "--no-policy")
    world.apm(root, "pack", "-o", str(out))
    return out / f"mcppkg-{version}"


def _approval_key(bundle: Path) -> str:
    info = detect_local_bundle(bundle)
    return local_bundle_approval_key(
        info.package_id, str(info.plugin_json.get("version") or ""), info.source_dir, info.lockfile
    )


def _set_executables(project: Path, executables: dict | None) -> None:
    manifest = load_yaml(project / "apm.yml")
    manifest.pop("executables", None)
    if executables is not None:
        manifest["executables"] = executables
    dump_yaml(manifest, project / "apm.yml")


def _servers(project: Path) -> list[str]:
    path = project / ".mcp.json"
    if not path.exists():
        return []
    return sorted(json.loads(path.read_text()).get("mcpServers", {}))


def _assert_retired(world: MarketplacePackWorld, project: Path) -> None:
    assert _servers(project) == []
    lock = world.lock(project)
    assert _SERVER not in (lock.get("mcp_config_provenance") or {}), lock
    assert _SERVER not in (lock.get("mcp_servers") or []), lock
    assert _SERVER not in (lock.get("mcp_configs") or {}), lock


def test_bundle_mcp_retires_on_revoke_deny_and_repack(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    producer = world.isolated.work_root / "mcppkg"
    out = tmp_path / "out"
    bundle = _pack(world, producer, out, "1.0.0", mcp=True)
    key = _approval_key(bundle)
    consumer = world.consumer("bundle-consumer", targets=("claude",))
    approved = {"allow": {key: {"mcp": True}}}

    _set_executables(consumer, approved)
    world.apm(consumer, "install", str(bundle), "--no-policy")
    assert _servers(consumer) == [_SERVER]
    assert world.lock(consumer)["mcp_config_provenance"] == {_SERVER: "bundle:mcppkg"}

    # Revoked approval: the reinstall parks the server and retires the old one.
    _set_executables(consumer, {})
    text = output(world.apm(consumer, "install", str(bundle), "--no-policy"))
    assert "Skipped 1 bundle MCP executable" in text, text
    _assert_retired(world, consumer)

    # Denied: the reinstall retires the server again.
    _set_executables(consumer, approved)
    world.apm(consumer, "install", str(bundle), "--no-policy")
    assert _servers(consumer) == [_SERVER]
    world.apm(consumer, "deny", key)
    world.apm(consumer, "install", str(bundle), "--no-policy")
    _assert_retired(world, consumer)

    # A newer bundle that ships no MCP retires the server; a dry run retires nothing.
    _set_executables(consumer, None)
    world.apm(consumer, "install", str(bundle), "--no-policy")
    assert _servers(consumer) == [_SERVER]
    mcpless = _pack(world, producer, out, "1.0.1", mcp=False)
    world.apm(consumer, "install", str(mcpless), "--no-policy", "--dry-run")
    assert _servers(consumer) == [_SERVER]
    world.apm(consumer, "install", str(mcpless), "--no-policy")
    _assert_retired(world, consumer)
    world.apm(consumer, "audit", "--ci", "--no-policy")
