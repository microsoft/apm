"""Declared config keys for self-defined MCP servers with slashes (#3456).

Exercises the real install path (``run_mcp_install`` -> ``SafeMCPInstaller``
-> every runtime adapter) against hermetic project and home directories.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.conflict_detector import MCPConflictDetector
from apm_cli.core.safe_installer import SafeMCPInstaller
from apm_cli.factory import ClientFactory
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.integration.mcp_integrator_install import run_mcp_install
from apm_cli.models.dependency.mcp import MCPDependency

ALL_RUNTIMES = sorted(ClientFactory.supported_clients())
# VS Code always keyed servers by their declared name, so it never wrote a
# truncated entry. OpenCode's detector reads ``mcpServers`` while the adapter
# writes ``mcp``, so APM cannot observe its existing keys.
LEGACY_RUNTIMES = [rt for rt in ALL_RUNTIMES if rt not in {"vscode", "opencode"}]
SIGNAL_DIRS = (
    ".agent",
    ".agents",
    ".claude",
    ".codex",
    ".cursor",
    ".gemini",
    ".github",
    ".idea",
    ".kiro",
    ".opencode",
    ".vscode",
    ".windsurf",
)
REVERSE_DNS = "com.example/remote-devops"
SAME_SEGMENT = "io.github.other/remote-devops"
LEGACY_KEY = "remote-devops"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a project root whose runtime configs all live under tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    for var, sub in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_DATA_HOME", ".local/share"),
        ("LOCALAPPDATA", "AppData/Local"),
        ("APPDATA", "AppData/Roaming"),
    ):
        monkeypatch.setenv(var, str(home / sub))
    for var in ("HERMES_HOME", "COPILOT_HOME", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "project"
    root.mkdir()
    for name in SIGNAL_DIRS:
        (root / name).mkdir()
    return root


def _dep(name: str, transport: str) -> MCPDependency:
    if transport == "http":
        return MCPDependency(name=name, registry=False, transport="http", url="https://e.test/mcp")
    return MCPDependency(
        name=name, registry=False, transport="stdio", command="npx", args=["-y", "srv"]
    )


def _written_keys(runtime: str, project_root: Path) -> set[str]:
    client = ClientFactory.create_client(runtime, project_root=project_root, user_scope=False)
    if runtime == "opencode":
        config = json.loads(Path(client.get_config_path()).read_text(encoding="utf-8"))
        return set(config.get("mcp", {}))
    return set(MCPConflictDetector(client).get_existing_server_configs())


def _seed_legacy_entry(runtime: str, project_root: Path, dep: MCPDependency | None = None) -> None:
    """Write the truncated entry older APM produced (or a user-edited one)."""
    client = ClientFactory.create_client(runtime, project_root=project_root, user_scope=False)
    if dep is None:
        client.update_config({LEGACY_KEY: {"command": "npx", "args": ["-y", "edited"]}})
    else:
        cache = {dep.name: MCPIntegrator._build_self_defined_info(dep)}
        assert client.configure_mcp_server(
            dep.name, server_name=LEGACY_KEY, server_info_cache=cache
        )
    assert LEGACY_KEY in _written_keys(runtime, project_root)


def _install(deps, runtime, project_root, managed=None, logger=None, stored=None):
    return run_mcp_install(
        deps,
        runtime=runtime,
        project_root=project_root,
        logger=logger or MagicMock(),
        managed_target_servers=managed,
        stored_mcp_configs=stored,
    )


@pytest.mark.parametrize("transport", ["http", "stdio"])
@pytest.mark.parametrize("runtime", ALL_RUNTIMES)
def test_reverse_dns_name_is_written_and_owned_under_declared_key(project, runtime, transport):
    managed: dict[str, set[str]] = {}

    assert _install([_dep(REVERSE_DNS, transport)], runtime, project, managed) == 1

    assert REVERSE_DNS in _written_keys(runtime, project)
    assert LEGACY_KEY not in _written_keys(runtime, project)
    # The ownership ledger persisted to mcp_target_servers names the written key.
    assert managed == {runtime: {REVERSE_DNS}}


@pytest.mark.parametrize("runtime", ALL_RUNTIMES)
def test_declared_names_sharing_final_segment_do_not_collide(project, runtime):
    deps = [_dep(REVERSE_DNS, "http"), _dep(SAME_SEGMENT, "stdio")]
    managed: dict[str, set[str]] = {}

    assert _install(deps, runtime, project, managed) == 2

    assert {REVERSE_DNS, SAME_SEGMENT} <= _written_keys(runtime, project)
    assert managed == {runtime: {REVERSE_DNS, SAME_SEGMENT}}


@pytest.mark.parametrize("runtime", ALL_RUNTIMES)
def test_scoped_and_plain_self_defined_names_keep_their_keys(project, runtime):
    deps = [_dep("@scope/server", "stdio"), _dep("plain-server", "http")]

    assert _install(deps, runtime, project) == 2

    assert {"@scope/server", "plain-server"} <= _written_keys(runtime, project)


@pytest.mark.parametrize("runtime", ALL_RUNTIMES)
def test_registry_backed_owner_repo_key_is_unchanged(project, runtime):
    ref = "microsoft/azure-devops-mcp"
    cache = {
        ref: {
            "name": ref,
            "remotes": [{"transport_type": "http", "url": "https://e.test/mcp"}],
        }
    }
    installer = SafeMCPInstaller(runtime, project_root=project)

    summary = installer.install_servers([ref], server_info_cache=cache)

    assert summary.installed == [ref]
    expected = ref if runtime == "vscode" else "azure-devops-mcp"
    assert expected in _written_keys(runtime, project)


@pytest.mark.parametrize("runtime", ALL_RUNTIMES)
def test_registry_lookup_failure_does_not_raise(project, runtime):
    installer = SafeMCPInstaller(runtime, project_root=project)
    adapter = installer.adapter
    with (
        patch.object(
            adapter.registry_client,
            "find_server_by_reference",
            side_effect=RuntimeError("registry unreachable"),
        ),
        patch.object(installer.conflict_detector, "check_server_exists", return_value=False),
    ):
        summary = installer.install_servers([REVERSE_DNS])

    assert summary.installed == []
    assert [item["server"] for item in summary.failed] == [REVERSE_DNS]


@pytest.mark.parametrize("runtime", [rt for rt in ALL_RUNTIMES if rt != "vscode"])
def test_adapter_fetch_failure_returns_false(project, runtime):
    adapter = ClientFactory.create_client(runtime, project_root=project, user_scope=False)
    with patch.object(adapter, "_fetch_server_info", side_effect=RuntimeError("lookup failed")):
        assert adapter.configure_mcp_server(REVERSE_DNS) is False


@pytest.mark.parametrize("runtime", LEGACY_RUNTIMES)
def test_owned_truncated_entry_is_replaced_by_declared_key(project, runtime):
    dep = _dep(REVERSE_DNS, "http")
    _seed_legacy_entry(runtime, project, dep)
    managed = {runtime: {REVERSE_DNS}}

    assert _install([dep], runtime, project, managed, stored={REVERSE_DNS: dep.to_dict()}) == 1

    keys = _written_keys(runtime, project)
    assert REVERSE_DNS in keys
    assert LEGACY_KEY not in keys


@pytest.mark.parametrize("runtime", LEGACY_RUNTIMES)
def test_unowned_truncated_entry_is_left_with_names_only_guidance(project, runtime):
    _seed_legacy_entry(runtime, project)
    logger = MagicMock()

    assert _install([_dep(REVERSE_DNS, "http")], runtime, project, {}, logger) == 1

    keys = _written_keys(runtime, project)
    assert {REVERSE_DNS, LEGACY_KEY} <= keys
    warnings = [call.args[0] for call in logger.warning.call_args_list]
    guidance = [w for w in warnings if f"'{LEGACY_KEY}'" in w]
    assert len(guidance) == 1
    assert REVERSE_DNS in guidance[0]
    assert "old" not in guidance[0]


def test_truncated_key_claimed_by_current_dependency_is_kept(project):
    _seed_legacy_entry("copilot", project)
    managed = {"copilot": {REVERSE_DNS, LEGACY_KEY}}
    logger = MagicMock()
    deps = [_dep(REVERSE_DNS, "http"), _dep(LEGACY_KEY, "stdio")]

    _install(deps, "copilot", project, managed, logger)

    assert {REVERSE_DNS, LEGACY_KEY} <= _written_keys("copilot", project)
    assert not any(f"'{LEGACY_KEY}'" in c.args[0] for c in logger.warning.call_args_list)


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_owned_but_edited_truncated_entry_is_left_on_baseline_runtimes(project, runtime):
    dep = _dep(REVERSE_DNS, "http")
    _seed_legacy_entry(runtime, project)
    logger = MagicMock()
    managed = {runtime: {REVERSE_DNS}}

    _install([dep], runtime, project, managed, logger, stored={REVERSE_DNS: dep.to_dict()})

    assert {REVERSE_DNS, LEGACY_KEY} <= _written_keys(runtime, project)
    guidance = [c.args[0] for c in logger.warning.call_args_list if f"'{LEGACY_KEY}'" in c.args[0]]
    assert len(guidance) == 1
    assert "edited" not in guidance[0]


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_reinstall_and_uninstall_use_declared_key_on_baseline_runtimes(project, runtime):
    dep = _dep(REVERSE_DNS, "http")
    managed: dict[str, set[str]] = {}
    hashes: dict[str, dict[str, str]] = {}
    stored = {REVERSE_DNS: dep.to_dict()}
    run_mcp_install(
        [dep],
        runtime=runtime,
        project_root=project,
        logger=MagicMock(),
        managed_target_servers=managed,
        native_hashes=hashes,
    )
    assert REVERSE_DNS in _written_keys(runtime, project)

    # Reinstall compares the declared-key entry: no native conflict is raised.
    run_mcp_install(
        [dep],
        runtime=runtime,
        project_root=project,
        logger=MagicMock(),
        managed_target_servers=managed,
        native_hashes=hashes,
        stored_mcp_configs=stored,
    )

    MCPIntegrator.remove_stale(
        {REVERSE_DNS},
        runtime=runtime,
        project_root=project,
        scope=None,
        stored_configs=stored,
        native_hashes=hashes,
        fail_on_write_error=True,
    )
    assert REVERSE_DNS not in _written_keys(runtime, project)
