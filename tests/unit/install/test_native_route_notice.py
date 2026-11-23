"""The native-route notice offers the loose route only where it exists (#4998).

Only a Claude-format plugin at project scope deploys loose under a skills:
selection. At user scope such a plugin parks, and an Agent Plugin always stays
on the native route, so their notice omits the hint.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.claude_plugins.admission import NATIVE_ROUTE_NOTICE, ClaudeNativePlugin
from apm_cli.core.scope import InstallScope
from apm_cli.install.phases.claude_plugins import _note_native_route
from apm_cli.native_plugins.scopes import COPILOT
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-claude-native-plugin-registration")]

_HINT = ' To deploy its skills loose instead, set skills: ["*"] on this dependency in apm.yml.'


def _plugins(tmp_path: Path) -> dict[str, ClaudeNativePlugin]:
    claude_root = tmp_path / "claude-kit"
    (claude_root / ".claude-plugin").mkdir(parents=True)
    (claude_root / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "claude-kit"}), encoding="utf-8"
    )
    agent_root = tmp_path / "agent-kit"
    agent_root.mkdir()
    (agent_root / "plugin.json").write_text(json.dumps({"name": "agent-kit"}), encoding="utf-8")
    return {
        "owner/claude-kit": ClaudeNativePlugin("owner/claude-kit", "claude-kit", claude_root, "h"),
        "owner/agent-kit": ClaudeNativePlugin(
            "owner/agent-kit", "agent-kit", agent_root, "h", harnesses=frozenset({COPILOT})
        ),
    }


def _notices(tmp_path: Path, scope: InstallScope) -> dict[str, str]:
    ctx = SimpleNamespace(
        diagnostics=DiagnosticCollector(), scope=scope, claude_native_plugins=_plugins(tmp_path)
    )
    _note_native_route(ctx)
    return {d.package: d.message for d in ctx.diagnostics.by_category()["warning"]}


def test_project_scope_hint_only_for_a_claude_format_plugin(tmp_path: Path) -> None:
    assert _notices(tmp_path, InstallScope.PROJECT) == {
        "owner/claude-kit": NATIVE_ROUTE_NOTICE + _HINT,
        "owner/agent-kit": NATIVE_ROUTE_NOTICE,
    }


def test_user_scope_never_offers_the_loose_route(tmp_path: Path) -> None:
    assert _notices(tmp_path, InstallScope.USER) == {
        "owner/claude-kit": NATIVE_ROUTE_NOTICE,
        "owner/agent-kit": NATIVE_ROUTE_NOTICE,
    }
