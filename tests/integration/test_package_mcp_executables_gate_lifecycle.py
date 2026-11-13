"""Package-declared self-defined MCP servers follow the executables gate (#3649).

When a project ``executables:`` block or org policy turns the executables gate
on, a self-defined (``registry: false``) MCP server declared by a dependency is
written only when its declaring package is trusted, and an org
``executables.deny_all`` keeps it out even when the project allows the package.
The approval is keyed by the package identity recorded in the lockfile, not by
the server name. Without a gate the server deploys as before.
"""

from __future__ import annotations

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
