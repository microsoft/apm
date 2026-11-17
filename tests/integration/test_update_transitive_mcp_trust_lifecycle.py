"""Transitive self-defined MCP trust is per invocation for update too (#3649).

``apm install`` deploys a self-defined (``registry: false``) MCP server from a
transitive package only with ``--trust-transitive-mcp``. ``apm update`` follows
the same rule: the lockfile records what was written, but it never grants
trust, so a server recorded in ``apm.lock.yaml`` is not deployed again unless
the update run passes ``--trust-transitive-mcp`` itself.
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


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _chain(tmp_path: Path, binary: Path) -> Chain:
    chain = Chain(tmp_path / "world", binary, dirs=(".claude",))
    _write(
        chain.project / "pkgs" / "parent" / "apm.yml",
        {"name": "parent", "version": "1.0.0", "dependencies": {"apm": ["../leaf"]}},
    )
    _write(
        chain.project / "pkgs" / "leaf" / "apm.yml",
        {
            "name": "leaf",
            "version": "1.0.0",
            "dependencies": {"mcp": [stdio_server("leaf-server", "x")]},
        },
    )
    chain.write_manifest([], apm=["./pkgs/parent"], targets=["claude"])
    return chain


def test_update_needs_its_own_transitive_mcp_trust(tmp_path: Path, apm_binary_path: Path) -> None:
    chain = _chain(tmp_path, apm_binary_path)

    chain.ok("install", "--trust-transitive-mcp")
    assert "leaf-server" in chain.servers("claude")
    trusted_lock = chain.lock()

    chain.ok("install")
    assert "leaf-server" not in chain.servers("claude")

    lock_path = chain.project / "apm.lock.yaml"
    lock = chain.lock()
    for key in ("mcp_servers", "mcp_configs", "mcp_target_servers", "mcp_config_provenance"):
        if key in trusted_lock:
            lock[key] = trusted_lock[key]
    lock_path.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")

    chain.ok("update", "--yes")
    assert "leaf-server" not in chain.servers("claude")

    chain.ok("update", "--yes", "--trust-transitive-mcp")
    assert "leaf-server" in chain.servers("claude")
