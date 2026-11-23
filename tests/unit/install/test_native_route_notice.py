"""The native-route notice offers the loose route only where it exists (#4998).

Only a Claude-format plugin with selectable skills, at project scope, deploys
loose under ``skills: ["*"]``. At user scope such a plugin parks, an Agent
Plugin (even one that also ships a Claude manifest) stays native, and a
commands-only plugin has no skill to select, so their notice omits the hint.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.claude_plugins.admission import NATIVE_ROUTE_NOTICE, ClaudeNativePlugin
from apm_cli.core.scope import InstallScope
from apm_cli.deps.plugin_parser import normalize_plugin_directory
from apm_cli.install.phases.claude_plugins import _note_native_route
from apm_cli.native_plugins.scopes import COPILOT
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-claude-native-plugin-registration")]

_HINT = ' To deploy its skills loose instead, set skills: ["*"] on this dependency in apm.yml.'


def _claude_root(root: Path, *, skills: bool) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": root.name}), encoding="utf-8"
    )
    if skills:
        (root / "skills/alpha").mkdir(parents=True)
        (root / "skills/alpha/SKILL.md").write_text("---\nname: alpha\n---\n", "utf-8")
    else:
        (root / "commands").mkdir()
        (root / "commands/hello.md").write_text("Say hello.\n", encoding="utf-8")
    # As install does: the parser records the plugin's selectable skills.
    normalize_plugin_directory(root, root / ".claude-plugin/plugin.json")
    return root


def _plugins(tmp_path: Path) -> dict[str, ClaudeNativePlugin]:
    skills_root = _claude_root(tmp_path / "skills-kit", skills=True)
    commands_root = _claude_root(tmp_path / "commands-kit", skills=False)
    # An Agent Plugin may also ship a Claude manifest; its admitted format decides.
    agent_root = _claude_root(tmp_path / "agent-kit", skills=True)
    return {
        "owner/skills-kit": ClaudeNativePlugin("owner/skills-kit", "skills-kit", skills_root, "h"),
        "owner/commands-kit": ClaudeNativePlugin(
            "owner/commands-kit", "commands-kit", commands_root, "h"
        ),
        "owner/agent-kit": ClaudeNativePlugin(
            "owner/agent-kit",
            "agent-kit",
            agent_root,
            "h",
            harnesses=frozenset({COPILOT}),
            agent_plugin=True,
        ),
    }


def _notices(tmp_path: Path, scope: InstallScope) -> dict[str, str]:
    ctx = SimpleNamespace(
        diagnostics=DiagnosticCollector(), scope=scope, claude_native_plugins=_plugins(tmp_path)
    )
    _note_native_route(ctx)
    return {d.package: d.message for d in ctx.diagnostics.by_category()["warning"]}


def test_project_scope_hint_only_for_a_claude_format_plugin_with_skills(
    tmp_path: Path,
) -> None:
    assert _notices(tmp_path, InstallScope.PROJECT) == {
        "owner/skills-kit": NATIVE_ROUTE_NOTICE + _HINT,
        "owner/commands-kit": NATIVE_ROUTE_NOTICE,
        "owner/agent-kit": NATIVE_ROUTE_NOTICE,
    }


def test_user_scope_never_offers_the_loose_route(tmp_path: Path) -> None:
    assert _notices(tmp_path, InstallScope.USER) == {
        "owner/skills-kit": NATIVE_ROUTE_NOTICE,
        "owner/commands-kit": NATIVE_ROUTE_NOTICE,
        "owner/agent-kit": NATIVE_ROUTE_NOTICE,
    }
