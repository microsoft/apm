"""Package-declared self-defined MCP servers follow the executables gate (#3649).

When a project ``executables:`` block or org policy turns the executables gate
on, a self-defined (``registry: false``) MCP server declared by a dependency is
written only when its declaring package is trusted, and an org
``executables.deny_all`` keeps it out even when the project allows the package.
The approval is keyed by the package identity recorded in the lockfile, not by
the server name. Without a gate the server deploys as before.
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

_PACKAGE = "./pkgs/tools"
_DENY_ALL_POLICY = "name: p\nversion: 1.0.0\nenforcement: block\nexecutables:\n  deny_all: true\n"


def _chain(tmp_path: Path, binary: Path, *, server: str = "tools-server") -> Chain:
    chain = Chain(tmp_path / "world", binary, dirs=(".claude",))
    package = chain.project / "pkgs" / "tools"
    package.mkdir(parents=True)
    manifest = {
        "name": "tools",
        "version": "1.0.0",
        "dependencies": {"mcp": [stdio_server(server, "x")]},
    }
    (package / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return chain


@pytest.mark.parametrize(
    ("executables", "expected"),
    [
        (None, True),
        ({}, False),
        ({"allow": {_PACKAGE: {"mcp": True}}}, True),
        ({"allow": {_PACKAGE: {"mcp": True}}, "deny": {_PACKAGE: {"mcp": True}}}, False),
    ],
    ids=["gate-off", "gate-on", "package-allowed", "package-denied"],
)
def test_package_mcp_server_follows_project_gate(
    tmp_path: Path, apm_binary_path: Path, executables: dict | None, expected: bool
) -> None:
    chain = _chain(tmp_path, apm_binary_path)
    extra = {} if executables is None else {"executables": executables}
    chain.write_manifest([], apm=[_PACKAGE], targets=["claude"], **extra)

    chain.ok("install", "--no-policy")

    assert ("tools-server" in chain.servers("claude")) is expected


def test_server_name_is_not_an_approval_key(tmp_path: Path, apm_binary_path: Path) -> None:
    chain = _chain(tmp_path, apm_binary_path, server="trusted/pkg")
    chain.write_manifest(
        [],
        apm=[_PACKAGE],
        targets=["claude"],
        executables={"allow": {"trusted/pkg": {"mcp": True}}},
    )

    chain.ok("install", "--no-policy")

    assert "trusted/pkg" not in chain.servers("claude")


def test_org_deny_all_keeps_package_mcp_server_out(tmp_path: Path, apm_binary_path: Path) -> None:
    chain = _chain(tmp_path, apm_binary_path)
    chain.seed_org_policy(_DENY_ALL_POLICY)
    chain.write_manifest(
        [],
        apm=[_PACKAGE],
        targets=["claude"],
        executables={"allow": {_PACKAGE: {"mcp": True}}},
    )

    chain.ok("install")

    assert "tools-server" not in chain.servers("claude")
    # Org deny wins over the project grant and is recorded as such (#3902).
    assert chain.lock()["mcp_omissions"]["tools-server"]["reason"] == "denied"

    # #3925: --frozen with org policy active replays the same gate and evidence.
    chain.ok("install", "--frozen")
    assert "tools-server" not in chain.servers("claude")


def test_user_scope_gate_parks_executables_and_frozen_agrees(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3954: ``executables: {}`` in APM_HOME/apm.yml gates ``install -g`` too."""
    chain = _chain(tmp_path, apm_binary_path)
    home = chain.isolated.home
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    hooky = chain.project / "pkgs" / "hooky"
    (hooky / ".apm" / "hooks").mkdir(parents=True)
    (hooky / "apm.yml").write_text("name: hooky\nversion: 1.0.0\n", encoding="utf-8")
    hook = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "echo hi-hooky"}]}
            ]
        }
    }
    (hooky / ".apm" / "hooks" / "hooky.json").write_text(json.dumps(hook), encoding="utf-8")
    manifest = {
        "name": "home",
        "version": "1.0.0",
        "targets": ["claude"],
        "dependencies": {"apm": [str(chain.project / "pkgs" / "tools"), str(hooky)]},
        "executables": {},
    }
    apm_home = home / ".apm"
    (apm_home / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    chain.ok("install", "-g", "--no-policy")

    claude_json = home / ".claude.json"
    servers = (
        json.loads(claude_json.read_text("utf-8")).get("mcpServers", {})
        if claude_json.exists()
        else {}
    )
    assert "tools-server" not in servers
    settings = home / ".claude" / "settings.json"
    assert not settings.exists() or "hi-hooky" not in settings.read_text("utf-8")
    lock = yaml.safe_load((apm_home / "apm.lock.yaml").read_text("utf-8"))
    assert "tools-server" in lock["mcp_omissions"]

    chain.ok("install", "-g", "--frozen", "--no-policy")
