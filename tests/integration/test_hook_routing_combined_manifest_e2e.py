"""Hermetic install-path proof for combined multi-target hook manifest routing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any accidental home-scoped writes inside the pytest temp tree."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


_UNIVERSAL_COPILOT_SHAPE_HOOKS = {
    "sessionStart": [
        {"type": "command", "bash": "node a.js", "powershell": "node a.js", "timeoutSec": 5}
    ]
}

_CLAUDE_SHAPE_HOOKS = {
    "SessionStart": [
        {
            "matcher": "startup",
            "hooks": [{"type": "command", "command": "node a.js", "timeout": 5}],
        }
    ],
    "SubagentStart": [{"hooks": [{"type": "command", "command": "node b.js", "timeout": 5}]}],
}

_COPILOT_SHAPE_HOOKS = {
    "sessionStart": [
        {"type": "command", "bash": "node a.js", "powershell": "node a.js", "timeoutSec": 5}
    ]
}


def _make_ponytail_package(tmp_path: Path, *, name: str = "ponytail") -> PackageInfo:
    """Build a package fixture shaped like the DietrichGebert/ponytail package.

    A universal Copilot-shaped hooks.json sits alongside a Claude-shaped
    combined manifest and a dedicated Copilot manifest, mirroring the
    real-world layout that triggered issue routing hooks-codex/-claude
    combined stems only to the last token.
    """
    package_root = tmp_path / "apm_modules" / "local" / name
    apm_hooks_dir = package_root / ".apm" / "hooks"
    apm_hooks_dir.mkdir(parents=True)
    (apm_hooks_dir / "hooks.json").write_text(
        json.dumps({"hooks": _UNIVERSAL_COPILOT_SHAPE_HOOKS}, indent=2),
        encoding="utf-8",
    )

    hooks_dir = package_root / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "claude-codex-hooks.json").write_text(
        json.dumps({"hooks": _CLAUDE_SHAPE_HOOKS}, indent=2),
        encoding="utf-8",
    )
    (hooks_dir / "copilot-hooks.json").write_text(
        json.dumps({"hooks": _COPILOT_SHAPE_HOOKS}, indent=2),
        encoding="utf-8",
    )

    package = APMPackage(name=name, version="1.0.0", source=f"DietrichGebert/{name}")
    return PackageInfo(package=package, install_path=package_root)


def _integrate_package_hooks(
    package_info: PackageInfo,
    project_root: Path,
    *,
    target_name: str,
) -> dict[str, Any]:
    """Run the install service dispatch that invokes HookIntegrator for a target."""
    return integrate_package_primitives(
        package_info,
        project_root,
        targets=[KNOWN_TARGETS[target_name]],
        integrators=IntegratorBundle(
            prompt=None,
            agent=None,
            skill=SkillIntegrator(),
            instruction=None,
            command=None,
            hook=HookIntegrator(),
        ),
        force=False,
        managed_files=set(),
        diagnostics=DiagnosticCollector(),
        package_name=package_info.package.name,
    )


def test_claude_target_gets_combined_manifest_not_universal_copilot_fallback(
    tmp_path: Path,
) -> None:
    """The combined claude-codex-hooks.json manifest must win over the universal file.

    Before the fix, a `claude-codex-hooks.json` manifest routed only to codex
    (matching the trailing `-codex-hooks` token), so the claude target had no
    target-specific file, fell back to the universal (Copilot-shaped)
    hooks.json, and Copilot field names leaked into .claude/settings.json.
    """
    project_root = tmp_path / "project"
    (project_root / ".claude").mkdir(parents=True)
    package_info = _make_ponytail_package(tmp_path)

    result = _integrate_package_hooks(package_info, project_root, target_name="claude")

    assert result["hooks"] == 1
    settings = json.loads((project_root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    hooks = settings["hooks"]
    assert set(hooks) == {"SessionStart", "SubagentStart"}

    raw = json.dumps(hooks)
    assert "sessionStart" not in raw
    assert "bash" not in raw
    assert "powershell" not in raw
    assert "timeoutSec" not in raw

    for entries in hooks.values():
        for entry in entries:
            assert "hooks" in entry


def test_copilot_target_still_deploys_its_own_manifest(tmp_path: Path) -> None:
    """The dedicated copilot-hooks.json file keeps deploying for the copilot target."""
    project_root = tmp_path / "project"
    (project_root / ".github").mkdir(parents=True)
    (project_root / ".github" / "copilot-instructions.md").write_text(
        "# Copilot instructions\n",
        encoding="utf-8",
    )
    package_info = _make_ponytail_package(tmp_path)

    result = _integrate_package_hooks(package_info, project_root, target_name="copilot")

    assert result["hooks"] == 1
    config_path = (
        project_root / ".github" / "hooks" / f"{package_info.package.name}-copilot-hooks.json"
    )
    assert config_path.exists()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(config["hooks"]) == {"sessionStart"}


def test_codex_target_still_gets_the_combined_manifest(tmp_path: Path) -> None:
    """The combined claude-codex-hooks.json manifest also deploys for the codex target."""
    project_root = tmp_path / "project"
    (project_root / ".codex").mkdir(parents=True)
    package_info = _make_ponytail_package(tmp_path)

    result = _integrate_package_hooks(package_info, project_root, target_name="codex")

    assert result["hooks"] == 1
    settings = json.loads((project_root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == {"SessionStart", "SubagentStart"}
