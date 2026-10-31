"""Tests for conservative legacy MCP ownership adoption."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.claude import ClaudeClientAdapter
from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.install.errors import RequiredIntegrationError
from apm_cli.install.mcp.ownership import (
    NativeMcpConnectionState,
    adopt_legacy_mcp_target_servers,
    compare_native_mcp_connection,
    migrate_legacy_project_target_servers,
    require_removable_mcp_connections,
)
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.dependency.mcp import MCPDependency

ManagedClaude = tuple[ClaudeClientAdapter, dict, dict]


@pytest.fixture
def managed_claude(tmp_path: Path) -> ManagedClaude:
    """Use real model rendering and native reads for the ownership decision."""
    client = ClaudeClientAdapter(project_root=tmp_path)
    stored = {
        "name": "managed",
        "registry": False,
        "transport": "stdio",
        "command": "python",
        "args": ["-m", "fixture_server"],
        "env": {"KEEP": "value"},
    }
    native = client.render_server_config(
        MCPIntegrator._build_self_defined_info(MCPDependency.from_dict(stored))
    )
    native.update(cwd="/fixture-cwd", oauthAccount={"accountUuid": "keep"})
    return client, stored, native


def _compare_managed_claude(
    client: ClaudeClientAdapter,
    stored: dict | None,
    native: dict,
    requested: dict | None,
    *,
    managed: bool = True,
) -> NativeMcpConnectionState:
    path = Path(client.get_config_path())
    path.write_text(json.dumps({"mcpServers": {"managed": native}}), encoding="utf-8")
    before = path.read_bytes()
    info = (
        MCPIntegrator._build_self_defined_info(MCPDependency.from_dict(requested))
        if requested is not None
        else None
    )
    result = compare_native_mcp_connection(
        client, "managed", requested_info=info, stored_config=stored, managed=managed
    )
    assert path.read_bytes() == before
    return result


def _remote_request() -> dict:
    return {
        "name": "managed",
        "registry": False,
        "transport": "http",
        "url": "https://example.invalid/mcp",
    }


@pytest.mark.parametrize("update", [False, True])
def test_native_only_cwd_keeps_verified_managed_state(
    managed_claude: ManagedClaude, update: bool
) -> None:
    client, stored, native = managed_claude
    requested = _remote_request() if update else deepcopy(stored)
    before = deepcopy((native, stored, requested))
    result = _compare_managed_claude(client, stored, native, requested)
    expected = (
        NativeMcpConnectionState.UPDATE_MANAGED
        if update
        else NativeMcpConnectionState.MATCHING_MANAGED
    )
    assert result is expected
    assert (native, stored, requested) == before


def test_removal_reads_one_snapshot_without_reusing_it_on_later_calls(
    managed_claude: ManagedClaude,
) -> None:
    client, stored, native = managed_claude
    native.pop("cwd")
    path = Path(client.get_config_path())
    path.write_text(
        json.dumps({"mcpServers": {"managed": native, "second": native}}), encoding="utf-8"
    )
    baselines = {"managed": stored, "second": {**stored, "name": "second"}}
    with patch.object(client, "get_current_config", wraps=client.get_current_config) as read:
        require_removable_mcp_connections(client, set(baselines), baselines)
        assert read.call_count == 1
        edited = {**native, "command": "personal"}
        path.write_text(
            json.dumps({"mcpServers": {"managed": native, "second": edited}}), encoding="utf-8"
        )
        before = path.read_bytes()
        with pytest.raises(RequiredIntegrationError, match="second"):
            require_removable_mcp_connections(client, set(baselines), baselines)
        assert read.call_count == 2
    assert path.read_bytes() == before


def test_explicit_empty_snapshot_is_not_replaced_by_a_native_read(
    managed_claude: ManagedClaude,
) -> None:
    client, stored, native = managed_claude
    _compare_managed_claude(client, stored, native, stored)
    with patch.object(client, "get_current_config", wraps=client.get_current_config) as read:
        state = compare_native_mcp_connection(
            client,
            "managed",
            requested_info=None,
            stored_config=stored,
            managed=True,
            existing_configs={},
        )
    read.assert_not_called()
    assert state is NativeMcpConnectionState.ABSENT


@pytest.mark.parametrize("native_cwd", ["/declared", "/edited", None])
def test_declared_cwd_remains_part_of_baseline(
    managed_claude: ManagedClaude, native_cwd: str | None
) -> None:
    client, stored, native = managed_claude
    stored["cwd"] = "/declared"
    if native_cwd is None:
        native.pop("cwd")
    else:
        native["cwd"] = native_cwd
    expected = (
        NativeMcpConnectionState.UPDATE_MANAGED
        if native_cwd == "/declared"
        else NativeMcpConnectionState.CONFLICT
    )
    assert _compare_managed_claude(client, stored, native, _remote_request()) is expected


def test_declared_cwd_can_be_updated_from_a_verified_baseline(
    managed_claude: ManagedClaude,
) -> None:
    client, stored, native = managed_claude
    stored["cwd"] = native["cwd"]
    requested = {**stored, "cwd": "/replacement"}
    result = _compare_managed_claude(client, stored, native, requested)
    assert result is NativeMcpConnectionState.UPDATE_MANAGED


@pytest.mark.parametrize("same", [False, True])
def test_request_cannot_acquire_native_only_cwd(managed_claude: ManagedClaude, same: bool) -> None:
    client, stored, native = managed_claude
    requested = {**stored, "cwd": "/fixture-cwd" if same else "/replacement"}
    expected = (
        NativeMcpConnectionState.MATCHING_FOREIGN if same else NativeMcpConnectionState.CONFLICT
    )
    assert _compare_managed_claude(client, stored, native, requested) is expected


@pytest.mark.parametrize("request_kind", ["repeat", "changed", "exact-native"])
def test_foreign_cwd_never_reaches_projection(
    managed_claude: ManagedClaude, request_kind: str
) -> None:
    client, stored, native = managed_claude
    requested = _remote_request() if request_kind == "changed" else dict(stored)
    if request_kind == "exact-native":
        requested["cwd"] = native["cwd"]
    with patch.object(
        client, "project_managed_server_config", wraps=client.project_managed_server_config
    ) as projection:
        result = _compare_managed_claude(client, stored, native, requested, managed=False)
    projection.assert_not_called()
    expected = (
        NativeMcpConnectionState.MATCHING_FOREIGN
        if request_kind == "exact-native"
        else NativeMcpConnectionState.CONFLICT
    )
    assert result is expected


@pytest.mark.parametrize("baseline_kind", ["missing", "invalid", "registry"])
def test_unverified_baseline_never_reaches_projection(
    managed_claude: ManagedClaude, baseline_kind: str
) -> None:
    client, stored, native = managed_claude
    baseline = {
        "missing": None,
        "invalid": {**stored, "cwd": 42},
        "registry": {"name": "managed"},
    }[baseline_kind]
    with patch.object(
        client, "project_managed_server_config", wraps=client.project_managed_server_config
    ) as projection:
        result = _compare_managed_claude(client, baseline, native, _remote_request())
    projection.assert_not_called()
    assert result is NativeMcpConnectionState.CONFLICT


def test_removal_never_projects_native_only_cwd(managed_claude: ManagedClaude) -> None:
    client, stored, native = managed_claude
    with patch.object(
        client, "project_managed_server_config", wraps=client.project_managed_server_config
    ) as projection:
        assert (
            _compare_managed_claude(client, stored, native, None)
            is NativeMcpConnectionState.CONFLICT
        )
        path = Path(client.get_config_path())
        before = path.read_bytes()
        with pytest.raises(RequiredIntegrationError, match="managed baseline"):
            require_removable_mcp_connections(client, {"managed"}, {"managed": stored})
    projection.assert_not_called()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("command", "personal"),
        ("args", ["personal"]),
        ("env", {"KEEP": "edited"}),
        ("url", "https://personal.invalid/mcp"),
        ("headers", {"X-Personal": "value"}),
        ("extra-key", {"keep": True}),
    ],
)
def test_projection_keeps_other_native_drift_visible(
    managed_claude: ManagedClaude, key: str, value: object
) -> None:
    client, stored, native = managed_claude
    native[key] = value
    assert (
        _compare_managed_claude(client, stored, native, _remote_request())
        is NativeMcpConnectionState.CONFLICT
    )


def test_failed_projected_baseline_does_not_project_foreign_matching(
    managed_claude: ManagedClaude,
) -> None:
    client, stored, native = managed_claude
    native["command"] = "personal"
    requested = {**stored, "command": "personal"}
    assert (
        _compare_managed_claude(client, stored, native, requested)
        is NativeMcpConnectionState.CONFLICT
    )


@pytest.mark.parametrize(
    "cwd",
    [None, True, 1, [], {}, "", " \t", "bad\x00path", "../other", r"..\other", "%2e%2e/other"],
)
def test_invalid_native_cwd_stays_a_conflict(managed_claude: ManagedClaude, cwd: object) -> None:
    client, stored, native = managed_claude
    native["cwd"] = cwd
    assert (
        _compare_managed_claude(client, stored, native, _remote_request())
        is NativeMcpConnectionState.CONFLICT
    )


def test_legacy_vscode_ownership_migrates_to_active_copilot_runtime() -> None:
    target_servers = {
        "vscode": {"managed", "shared"},
        "copilot": {"existing", "shared"},
    }

    migrate_legacy_project_target_servers(
        target_servers,
        active_runtimes={"copilot", "codex"},
        user_scope=False,
    )

    assert target_servers == {
        "copilot": {"existing", "managed", "shared"},
    }


@pytest.mark.parametrize(
    ("active_runtimes", "user_scope"),
    [
        ({"vscode"}, False),
        ({"copilot"}, True),
    ],
)
def test_legacy_vscode_ownership_stays_for_native_or_user_scope(
    active_runtimes: set[str],
    user_scope: bool,
) -> None:
    target_servers = {"vscode": {"managed"}}

    migrate_legacy_project_target_servers(
        target_servers,
        active_runtimes=active_runtimes,
        user_scope=user_scope,
    )

    assert target_servers == {"vscode": {"managed"}}


@pytest.mark.parametrize(
    ("native_config", "expected"),
    [
        ({"command": "echo", "args": ["managed"]}, {"codex": {"managed"}}),
        ({"command": "user-edited", "args": ["managed"]}, {}),
    ],
)
def test_legacy_adoption_requires_exact_native_baseline(
    tmp_path,
    native_config,
    expected,
) -> None:
    """User-edited native entries are never guessed to be APM-owned."""
    client = SimpleNamespace(
        mcp_servers_key="mcp_servers",
        get_current_config=lambda: {"mcp_servers": {"managed": native_config}},
        render_server_config=lambda _info: {
            "command": "echo",
            "args": ["managed"],
        },
    )
    stored = {
        "managed": {
            "name": "managed",
            "registry": False,
            "transport": "stdio",
            "command": "echo",
            "args": ["managed"],
        }
    }
    with (
        patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["codex"]),
        patch("apm_cli.factory.ClientFactory.create_client", return_value=client),
    ):
        adopted = adopt_legacy_mcp_target_servers(
            server_names={"managed"},
            stored_configs=stored,
            project_root=tmp_path,
            user_scope=False,
        )

    assert adopted == expected


@pytest.mark.parametrize(
    ("command", "enabled", "expected"),
    [
        (["echo", "managed"], True, {"opencode": {"managed"}}),
        (["user-edited", "managed"], True, {}),
        (["echo", "managed"], 1, {}),
    ],
)
def test_legacy_adoption_compares_native_opencode_entries(
    tmp_path, command, enabled, expected
) -> None:
    """OpenCode baselines use the ``mcp`` entry shape APM writes."""
    config = tmp_path / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    entry = {"type": "local", "enabled": enabled, "command": command}
    config.write_text(json.dumps({"mcp": {"managed": entry}}), encoding="utf-8")
    stored = {
        "managed": {
            "name": "managed",
            "registry": False,
            "transport": "stdio",
            "command": "echo",
            "args": ["managed"],
        }
    }
    with (
        patch.object(Path, "home", return_value=tmp_path),
        patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["opencode"]),
    ):
        adopted = adopt_legacy_mcp_target_servers(
            server_names={"managed"},
            stored_configs=stored,
            project_root=tmp_path,
            user_scope=True,
        )

    assert adopted == expected


def test_legacy_adoption_inspects_intellij_obsolete_config(tmp_path) -> None:
    """Exact legacy IntelliJ entries may be adopted for path migration."""
    baseline = {"command": "echo", "args": ["managed"]}
    client = SimpleNamespace(
        target_name="intellij",
        mcp_servers_key="servers",
        get_current_config=lambda: {"servers": {}},
        get_legacy_current_config=lambda: {"servers": {"managed": baseline}},
        render_server_config=lambda _info: baseline,
    )
    stored = {
        "managed": {
            "name": "managed",
            "registry": False,
            "transport": "stdio",
            "command": "echo",
            "args": ["managed"],
        }
    }
    with (
        patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["intellij"]),
        patch("apm_cli.factory.ClientFactory.create_client", return_value=client),
    ):
        adopted = adopt_legacy_mcp_target_servers(
            server_names={"managed"},
            stored_configs=stored,
            project_root=tmp_path,
            user_scope=False,
        )

    assert adopted == {"intellij": {"managed"}}


_FORWARDED_STDIO = {
    "name": "managed",
    "registry": False,
    "transport": "stdio",
    "command": "python",
    "args": ["-m", "fixture_server"],
    "env": {"MY_TOKEN": "${MY_TOKEN}"},
}


def _compare_codex(tmp_path: Path, written_value: str) -> NativeMcpConnectionState:
    """Compare a Codex entry the older formatter wrote with its current rendering."""
    client = CodexClientAdapter(project_root=tmp_path)
    requested = MCPIntegrator._build_self_defined_info(MCPDependency.from_dict(_FORWARDED_STDIO))
    native = deepcopy(client.render_server_config(requested))
    native.pop("env_vars")
    native["env"] = {"MY_TOKEN": written_value}
    return compare_native_mcp_connection(
        client,
        "managed",
        requested_info=requested,
        stored_config=_FORWARDED_STDIO,
        managed=True,
        existing_configs={"managed": native},
    )


@pytest.mark.parametrize("written_value", ["${MY_TOKEN}", "resolved-value"])
def test_codex_entry_from_older_formatter_upgrades_as_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, written_value: str
) -> None:
    monkeypatch.setenv("MY_TOKEN", "resolved-value")
    assert _compare_codex(tmp_path, written_value) is NativeMcpConnectionState.UPDATE_MANAGED


def test_codex_entry_with_manual_edit_stays_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_TOKEN", "resolved-value")
    assert _compare_codex(tmp_path, "edited-by-hand") is NativeMcpConnectionState.CONFLICT
