"""Recorded MCP servers leave native configs when their opt-in dir is pruned (#3664).

A dependency that ships one primitive plus a self-defined MCP server is the
only content in the runtime's opt-in directory (``.claude/`` or
``.opencode/``). Removing the dependency prunes that emptied directory before
MCP cleanup runs; the server recorded for the runtime must still leave the
project config while a user-authored server stays.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.integration._mcp_chain_kit import Chain, stdio_server

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_USER = {"claude": {"command": "mine"}, "opencode": {"type": "local", "command": ["mine"]}}
_PRIMITIVES = {
    "claude": ("skills/s/SKILL.md", "---\nname: s\ndescription: s\n---\ns\n"),
    "opencode": ("agents/rev.agent.md", "---\nname: rev\ndescription: r\n---\nreview\n"),
}


def _seed(chain: Chain, target: str) -> None:
    package = chain.project / "pkgs" / "p"
    relative, content = _PRIMITIVES[target]
    primitive = package / ".apm" / relative
    primitive.parent.mkdir(parents=True)
    primitive.write_text(content, encoding="utf-8")
    manifest = {"name": "p", "version": "1.0.0", "dependencies": {"mcp": [stdio_server("p", "p")]}}
    (package / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    config, key = {"claude": (".mcp.json", "mcpServers"), "opencode": ("opencode.json", "mcp")}[
        target
    ]
    (chain.project / config).write_text(json.dumps({key: {"mine": _USER[target]}}), "utf-8")


@pytest.mark.parametrize("removal", ["uninstall", "manifest-edit"])
@pytest.mark.parametrize("target", ["claude", "opencode"])
def test_recorded_server_leaves_config_after_opt_in_dir_is_pruned(
    tmp_path: Path, apm_binary_path: Path, target: str, removal: str
) -> None:
    opt_in = f".{target}"
    chain = Chain(tmp_path / f"{target}-{removal}", apm_binary_path, dirs=(opt_in, ".vscode"))
    _seed(chain, target)
    chain.write_manifest([], apm=["./pkgs/p"], targets=[target, "vscode"])

    chain.ok("install", "--no-policy", "--trust-transitive-mcp")
    assert set(chain.servers(target)) == {"mine", "p"}
    assert chain.lock()["mcp_target_servers"][target] == ["p"]

    if removal == "uninstall":
        chain.ok("uninstall", "./pkgs/p")
    else:
        chain.write_manifest([], targets=[target, "vscode"])
        chain.ok("install", "--no-policy", "--trust-transitive-mcp")

    assert not (chain.project / opt_in).exists(), "fixture must exercise the pruned directory"
    assert chain.servers(target) == {"mine": _USER[target]}
    assert chain.servers("vscode") == {}
    assert not chain.lock().get("mcp_servers")
    chain.ok("audit", "--ci", "--no-policy")
