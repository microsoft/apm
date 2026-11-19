"""Per-dependency ``targets:`` scopes MCP servers and per-target pruning (#2606).

``targets`` on a ``dependencies.mcp`` entry selects which targets receive the
server. It is never written into a native MCP config, and a target-scoped
install keeps servers that are still declared for the targets it skips.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from tests.integration._mcp_chain_kit import Chain, stdio_server

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]


def _scoped(name: str, *targets: str) -> dict:
    return {**stdio_server(name, name), "targets": list(targets)}


def test_project_dependency_targets_scope_servers_per_target(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = Chain(tmp_path / "project", apm_binary_path, dirs=(".claude", ".cursor"))
    chain.write_manifest(
        [_scoped("srv-claude-only", "claude"), _scoped("srv-cursor-only", "cursor")],
        targets=["claude", "cursor"],
    )
    result = chain.ok("install", "--no-policy")
    assert "preserved in extra" not in result.stdout + result.stderr

    assert set(chain.servers("claude")) == {"srv-claude-only"}
    assert set(chain.servers("cursor")) == {"srv-cursor-only"}
    for target in ("claude", "cursor"):
        for entry in chain.servers(target).values():
            assert "targets" not in entry, target
    assert chain.lock()["mcp_target_servers"] == {
        "claude": ["srv-claude-only"],
        "cursor": ["srv-cursor-only"],
    }

    # Retargeting a server prunes it only from the target it no longer selects.
    chain.write_manifest(
        [_scoped("srv-claude-only", "cursor"), _scoped("srv-cursor-only", "cursor")],
        targets=["claude", "cursor"],
    )
    chain.ok("install", "--no-policy")
    assert chain.servers("claude") == {}
    assert set(chain.servers("cursor")) == {"srv-claude-only", "srv-cursor-only"}
    chain.ok("install", "--no-policy", "--frozen")


def test_global_target_scoped_install_keeps_servers_declared_for_other_targets(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = Chain(tmp_path / "global", apm_binary_path)
    home = chain.isolated.home
    for directory in (".claude", ".codex"):
        (home / directory).mkdir(parents=True, exist_ok=True)
    apm_home = Path(chain.env["APM_HOME"])
    apm_home.mkdir(parents=True, exist_ok=True)

    def write_global(mcp: list[dict]) -> None:
        manifest = {"name": "global", "version": "1.0.0", "dependencies": {"mcp": mcp}}
        (apm_home / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    def claude_servers() -> dict:
        path = home / ".claude.json"
        return json.loads(path.read_text("utf-8")).get("mcpServers", {}) if path.exists() else {}

    write_global([stdio_server("srv-a", "a")])
    chain.ok("install", "-g", "--no-policy", "--only", "mcp", "--target", "claude")
    assert set(claude_servers()) == {"srv-a"}

    write_global([_scoped("srv-a", "claude"), _scoped("srv-b", "codex")])
    result = chain.ok("install", "-g", "--no-policy", "--only", "mcp", "--target", "codex")
    assert "Removed stale MCP server 'srv-a'" not in result.stdout
    assert set(claude_servers()) == {"srv-a"}

    codex_text = (home / ".codex" / "config.toml").read_text("utf-8")
    codex_names = {
        name
        for name in re.findall(r"^\[mcp_servers\.([^\].]+)\]$", codex_text, re.M)
        if not name.endswith(".env")
    }
    assert codex_names == {"srv-b"}
    assert not re.search(r"^targets\s*=", codex_text, re.M)
    lock = yaml.safe_load((apm_home / "apm.lock.yaml").read_text("utf-8"))
    assert lock["mcp_target_servers"] == {"claude": ["srv-a"], "codex": ["srv-b"]}
