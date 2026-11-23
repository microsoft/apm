"""The native-route notice offers the loose route only where it exists (#4998, #4995).

A Claude-format plugin deploys loose under ``native: false`` at both scopes,
commands included; an Agent Plugin (even one that also ships a Claude
manifest) has no loose route, so its notice omits the hint.
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

_HINT = " To deploy it loose instead, set native: false on this dependency in apm.yml."


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


def _notices(tmp_path: Path, scope: InstallScope, capsys) -> tuple[dict[str, str], str]:
    """The recorded notice per plugin, and the install summary as printed."""
    ctx = SimpleNamespace(
        diagnostics=DiagnosticCollector(), scope=scope, claude_native_plugins=_plugins(tmp_path)
    )
    _note_native_route(ctx)
    recorded = {d.package: d.message for d in ctx.diagnostics.by_category()["warning"]}
    ctx.diagnostics.render_summary()
    return recorded, " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize(
    "scope", [InstallScope.PROJECT, InstallScope.USER], ids=["project", "user"]
)
def test_every_claude_format_plugin_gets_the_hint_at_both_scopes(
    tmp_path: Path, capsys, scope: InstallScope
) -> None:
    recorded, shown = _notices(tmp_path, scope, capsys)
    assert recorded == {
        "owner/skills-kit": NATIVE_ROUTE_NOTICE + _HINT,
        "owner/commands-kit": NATIVE_ROUTE_NOTICE + _HINT,
        "owner/agent-kit": NATIVE_ROUTE_NOTICE,
    }
    assert shown.count("[!] [owner/") == 3, shown
    assert shown.count(" ".join(_HINT.split())) == 2, shown
