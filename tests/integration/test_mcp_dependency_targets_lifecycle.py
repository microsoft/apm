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

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile
from tests.integration._mcp_chain_kit import Chain, stdio_server

pytestmark = [
    pytest.mark.e2e,
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

    # An entry whose targets miss every active target is skipped, with a warning.
    chain.write_manifest(
        [_scoped("srv-claude-only", "cursor"), _scoped("srv-codex-only", "codex")],
        targets=["claude", "cursor"],
    )
    result = chain.ok("install", "--no-policy")
    output = " ".join((result.stdout + result.stderr).split())
    assert "srv-codex-only: MCP server targets [codex] do not overlap" in output
    assert "srv-codex-only" not in chain.servers("claude")
    assert "srv-codex-only" not in chain.servers("cursor")
    assert chain.lock()["mcp_target_servers"] == {"cursor": ["srv-claude-only"]}

    # A target the manifest itself drops leaves every entry's reach.
    chain.write_manifest(
        [_scoped("srv-claude-only", "cursor"), _scoped("srv-cursor-only", "cursor")],
        targets=["claude"],
    )
    chain.ok("install", "--no-policy")
    assert chain.servers("cursor") == {}
    assert chain.lock().get("mcp_target_servers", {}) == {}


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

    write_global([stdio_server("srv-a", "a"), stdio_server("srv-c", "c")])
    chain.ok("install", "-g", "--no-policy", "--only", "mcp", "--target", "claude")
    assert set(claude_servers()) == {"srv-a", "srv-c"}
    lock_path = apm_home / "apm.lock.yaml"
    receipt = DeploymentLedgerCodec.mcp_native_hashes(LockFile.read(lock_path))["claude"]["srv-a"]

    # An untargeted server follows the run's --target; a scoped one keeps its targets.
    write_global(
        [_scoped("srv-a", "claude"), _scoped("srv-b", "codex"), stdio_server("srv-c", "c")]
    )
    result = chain.ok("install", "-g", "--no-policy", "--only", "mcp", "--target", "codex")
    assert "Removed stale MCP server 'srv-a'" not in result.stdout
    assert set(claude_servers()) == {"srv-a"}

    codex_text = (home / ".codex" / "config.toml").read_text("utf-8")
    codex_names = {
        name
        for name in re.findall(r"^\[mcp_servers\.([^\].]+)\]$", codex_text, re.M)
        if not name.endswith(".env")
    }
    assert codex_names == {"srv-b", "srv-c"}
    assert not re.search(r"^targets\s*=", codex_text, re.M)
    lock = yaml.safe_load(lock_path.read_text("utf-8"))
    assert lock["mcp_target_servers"] == {"claude": ["srv-a"], "codex": ["srv-b", "srv-c"]}
    kept = DeploymentLedgerCodec.mcp_native_hashes(LockFile.read(lock_path))["claude"]
    assert kept["srv-a"] == receipt

    # Undeclaring the server still retires it from the target outside this run.
    write_global([_scoped("srv-b", "codex"), stdio_server("srv-c", "c")])
    chain.ok("install", "-g", "--no-policy", "--only", "mcp", "--target", "codex")
    assert claude_servers() == {}


def test_project_retained_claims_survive_exclude_retarget_and_pending_updates(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Exclusion keeps claims, skipped targets get later updates, shared-file retarget keeps."""
    chain = Chain(tmp_path / "retained", apm_binary_path, dirs=(".claude", ".cursor"))
    manifest_targets = ["claude", "cursor", "copilot"]
    chain.write_manifest(
        [_scoped("srv-cursor", "cursor"), _scoped("srv-shared", "claude")],
        targets=manifest_targets,
    )
    chain.ok("install", "--no-policy")
    assert chain.servers("cursor")["srv-cursor"]["args"] == ["srv-cursor"]

    # --exclude skips a target the manifest still lists: its claims stay.
    chain.ok("install", "--no-policy", "--exclude", "cursor")
    assert "srv-cursor" in chain.servers("cursor")
    assert chain.lock()["mcp_target_servers"]["cursor"] == ["srv-cursor"]

    # A declaration change made while cursor is skipped lands on its next install.
    updated = {**_scoped("srv-cursor", "cursor"), "args": ["srv-cursor", "--v2"]}
    chain.write_manifest([updated, _scoped("srv-shared", "claude")], targets=manifest_targets)
    chain.ok("install", "--no-policy", "--exclude", "cursor")
    assert chain.servers("cursor")["srv-cursor"]["args"] == ["srv-cursor"]
    chain.ok("install", "--no-policy")
    assert chain.servers("cursor")["srv-cursor"]["args"] == ["srv-cursor", "--v2"]

    # Retargeting between claude and copilot keeps the one shared .mcp.json entry.
    chain.write_manifest([updated, _scoped("srv-shared", "copilot")], targets=manifest_targets)
    chain.ok("install", "--no-policy")
    assert "srv-shared" in chain.servers("copilot")
    assert chain.lock()["mcp_target_servers"].get("claude", []) == []
    assert chain.lock()["mcp_target_servers"]["copilot"] == ["srv-shared"]


@pytest.mark.parametrize(
    ("spelling", "alias"),
    (("copilot", False), ("Copilot", False), ("vscode", True), ("VSCode", True)),
)
def test_project_exclude_reads_like_target(
    tmp_path: Path, apm_binary_path: Path, spelling: str, alias: bool
) -> None:
    """Project ``--exclude`` folds case and honours aliases like ``--target`` (C4, #3967)."""
    chain = Chain(tmp_path / "exclude", apm_binary_path, dirs=(".cursor",))
    chain.write_manifest([stdio_server("srv", "srv")], targets=["copilot", "cursor"])
    result = chain.ok("install", "--no-policy", "--exclude", spelling)
    output = " ".join((result.stdout + result.stderr).split())
    assert output.count("is deprecated and will") == (1 if alias else 0)
    assert chain.servers("copilot") == {}
    assert set(chain.servers("cursor")) == {"srv"}
    assert set(chain.lock()["mcp_target_servers"]) == {"cursor"}


def test_project_exclude_unknown_value_warns_and_alias_keeps_exit_code(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """An unknown ``--exclude`` warns; alias case spellings share one exit code (C4)."""
    chain = Chain(tmp_path / "unknown", apm_binary_path)
    chain.write_manifest([stdio_server("srv", "srv")], targets=["copilot"])
    result = chain.ok("install", "--no-policy", "--exclude", "bogus")
    output = " ".join((result.stdout + result.stderr).split())
    assert output.count("names no target or MCP runtime") == 1
    assert set(chain.servers("copilot")) == {"srv"}

    codes = {
        chain.run("install", "--no-policy", "--exclude", v).returncode for v in ("vscode", "VSCode")
    }
    assert len(codes) == 1, codes


def test_removing_the_last_mcp_server_is_reported_as_a_change(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The install summary counts an MCP removal; it never says nothing changed (#3989)."""
    chain = Chain(tmp_path / "project", apm_binary_path, dirs=(".claude",))
    chain.write_manifest([stdio_server("fetch", "fetch")], targets=["claude"])
    chain.ok("install", "--no-policy")
    assert set(chain.servers("claude")) == {"fetch"}

    chain.write_manifest([], targets=["claude"])
    result = chain.ok("install", "--no-policy")
    text = result.stdout + result.stderr
    assert chain.servers("claude") == {}
    assert "Removed 1 stale MCP server" in text, text
    assert "No changes" not in text, text
