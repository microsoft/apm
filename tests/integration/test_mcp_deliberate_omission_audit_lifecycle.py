"""Install, update and audit agree on deliberately omitted package MCP (#3720).

``apm install`` parks a package's self-defined MCP server while the executables
gate does not approve the package, and ``apm update`` retires a transitive
self-defined server when the run lacks ``--trust-transitive-mcp``. Both record
declaration-bound omission evidence, so ``apm audit --ci`` passes
``config-consistency``; missing evidence still fails, and a failed retirement
keeps the previous baseline instead of recording an omission.
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


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _audit(chain: Chain) -> dict:
    report = chain.isolated.root / "audit.json"
    result = chain.run(
        "audit", "--ci", "--no-policy", "--no-fail-fast", "-f", "json", "--output", str(report)
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check = next(item for item in payload["checks"] if item["name"] == "config-consistency")
    assert result.returncode == (0 if check["passed"] else 1), payload
    return check


def _drop_omission(chain: Chain, name: str) -> None:
    lock_path = chain.project / "apm.lock.yaml"
    lock = chain.lock()
    lock["mcp_omissions"].pop(name)
    lock_path.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")


def test_executables_gate_parks_server_with_audit_evidence(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = Chain(tmp_path / "world", apm_binary_path, dirs=(".claude",))
    _write(
        chain.project / "pkgs" / "tools" / "apm.yml",
        {
            "name": "tools",
            "version": "1.0.0",
            "dependencies": {"mcp": [stdio_server("tools-server", "x")]},
        },
    )
    chain.write_manifest([], apm=[_PACKAGE], targets=["claude"], executables={})

    chain.ok("install", "--no-policy")
    assert "tools-server" not in chain.servers("claude")
    lock = chain.lock()
    assert lock["mcp_omissions"]["tools-server"]["reason"] == "unapproved-executables"
    assert "tools-server" not in (lock.get("mcp_configs") or {})
    assert _audit(chain)["passed"], "parked server must not fail config-consistency"

    chain.ok("install", "--no-policy")
    assert _audit(chain)["passed"]

    _drop_omission(chain, "tools-server")
    check = _audit(chain)
    assert not check["passed"]
    assert any("selection is unknown" in detail for detail in check["details"])

    chain.write_manifest(
        [],
        apm=[_PACKAGE],
        targets=["claude"],
        executables={"allow": {_PACKAGE: {"mcp": True}}},
    )
    chain.ok("install", "--no-policy")
    assert "tools-server" in chain.servers("claude")
    assert "tools-server" not in (chain.lock().get("mcp_omissions") or {})
    assert _audit(chain)["passed"]

    chain.write_manifest([], apm=[_PACKAGE], targets=["claude"], executables={})
    chain.ok("install", "--no-policy")
    assert "tools-server" not in chain.servers("claude")
    lock = chain.lock()
    assert "tools-server" not in (lock.get("mcp_configs") or {})
    assert "tools-server" not in (lock.get("mcp_servers") or [])
    assert lock["mcp_omissions"]["tools-server"]["reason"] == "unapproved-executables"
    assert _audit(chain)["passed"], "revoked approval must retire the server with evidence"


def _transitive_chain(tmp_path: Path, binary: Path) -> Chain:
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
    chain.ok("install", "--trust-transitive-mcp")
    assert "leaf-server" in chain.servers("claude")
    assert _audit(chain)["passed"]
    return chain


@pytest.mark.parametrize("command", [("update", "--yes"), ("install",)])
def test_retiring_trusted_transitive_server_records_audit_evidence(
    tmp_path: Path, apm_binary_path: Path, command: tuple[str, ...]
) -> None:
    chain = _transitive_chain(tmp_path, apm_binary_path)

    chain.ok(*command)
    assert "leaf-server" not in chain.servers("claude")
    lock = chain.lock()
    assert lock["mcp_omissions"]["leaf-server"]["reason"] == "untrusted-transitive"
    assert "leaf-server" not in (lock.get("mcp_servers") or [])
    assert _audit(chain)["passed"], "retired transitive server must not fail config-consistency"

    _drop_omission(chain, "leaf-server")
    assert not _audit(chain)["passed"]

    chain.ok(*command, "--trust-transitive-mcp")
    assert "leaf-server" in chain.servers("claude")
    assert "leaf-server" not in (chain.lock().get("mcp_omissions") or {})
    assert _audit(chain)["passed"]


def test_failed_retirement_keeps_prior_baseline(tmp_path: Path, apm_binary_path: Path) -> None:
    chain = _transitive_chain(tmp_path, apm_binary_path)
    (chain.project / ".mcp.json").write_text("{not json", encoding="utf-8")

    result = chain.run("update", "--yes")

    assert result.returncode != 0
    lock = chain.lock()
    assert "leaf-server" in lock["mcp_configs"]
    assert "leaf-server" not in (lock.get("mcp_omissions") or {})
    assert _audit(chain)["passed"]
