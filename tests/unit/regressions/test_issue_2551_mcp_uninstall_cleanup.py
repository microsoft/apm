import json

import pytest

from apm_cli.adapters.client.intellij import _strip_jsonc_comments
from apm_cli.commands.uninstall.engine import (
    MCPUninstallCleanupError,
    _remove_stale_mcp_from_recorded_targets,
)
from apm_cli.install.mcp import ownership
from apm_cli.integration.mcp_integrator import MCPIntegrator


class _Lockfile:
    def __init__(self) -> None:
        self._mcp_target_servers_present = True
        self.mcp_target_servers = {
            "claude": ["server-a"],
            "cursor": ["server-b"],
        }
        self.mcp_configs = {}


def test_jsonc_cleanup_accepts_comments_without_corrupting_urls():
    raw = """{
      // managed servers
      "servers": {
        "docs": {"url": "https://example.com/mcp"} /* keep URL intact */
      }
    }"""

    data = json.loads(_strip_jsonc_comments(raw))

    assert data["servers"]["docs"]["url"] == "https://example.com/mcp"


def test_cleanup_only_touches_recorded_runtime_owners(monkeypatch):
    calls = []

    def fake_remove(stale, **kwargs):
        calls.append((set(stale), kwargs.get("runtime")))

    monkeypatch.setattr(MCPIntegrator, "remove_stale", fake_remove)

    _remove_stale_mcp_from_recorded_targets(
        {"server-a"},
        _Lockfile(),
        project_root=None,
        user_scope=False,
        scope=None,
    )

    assert calls == [({"server-a"}, "claude")]


def test_cleanup_continues_other_targets_before_reporting_failure(monkeypatch):
    calls = []

    def fake_remove(stale, **kwargs):
        runtime = kwargs.get("runtime")
        calls.append(runtime)
        if runtime == "claude":
            raise OSError("broken config")

    monkeypatch.setattr(MCPIntegrator, "remove_stale", fake_remove)

    with pytest.raises(
        MCPUninstallCleanupError,
        match="MCP cleanup failed for 1 target: claude: broken config",
    ):
        _remove_stale_mcp_from_recorded_targets(
            {"server-a", "server-b"},
            _Lockfile(),
            project_root=None,
            user_scope=False,
            scope=None,
        )

    assert calls == ["claude", "cursor"]


def test_cleanup_with_explicit_empty_ownership_does_not_scan_runtimes(monkeypatch):
    lockfile = _Lockfile()
    lockfile.mcp_target_servers = {}
    adoption_calls = []
    calls = []
    monkeypatch.setattr(
        ownership,
        "adopt_legacy_mcp_target_servers",
        lambda **kwargs: adoption_calls.append(kwargs) or {"intellij": {"server-a"}},
    )
    monkeypatch.setattr(
        MCPIntegrator,
        "remove_stale",
        lambda *_args, **_kwargs: calls.append("called"),
    )

    _remove_stale_mcp_from_recorded_targets(
        {"server-a"},
        lockfile,
        project_root=None,
        user_scope=False,
        scope=None,
    )

    assert adoption_calls == []
    assert calls == []


def test_cleanup_adopts_only_exact_legacy_owners(monkeypatch):
    lockfile = _Lockfile()
    lockfile._mcp_target_servers_present = False
    lockfile.mcp_target_servers = {}
    calls = []
    monkeypatch.setattr(
        ownership,
        "adopt_legacy_mcp_target_servers",
        lambda **_kwargs: {"claude": {"server-a"}},
    )
    monkeypatch.setattr(
        MCPIntegrator,
        "remove_stale",
        lambda stale, **kwargs: calls.append((set(stale), kwargs["runtime"])),
    )

    _remove_stale_mcp_from_recorded_targets(
        {"server-a", "server-b"},
        lockfile,
        project_root=None,
        user_scope=False,
        scope=None,
    )

    assert calls == [({"server-a"}, "claude")]
