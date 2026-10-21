"""Combined legacy descriptors require migration before install dispatch writes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apm_cli.hook_contract import HookContractError
from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = [pytest.mark.component, pytest.mark.integration]


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
    return _integrate_package_hooks_for_targets(package_info, project_root, [target_name])


def _integrate_package_hooks_for_targets(
    package_info: PackageInfo,
    project_root: Path,
    target_names: list[str],
) -> dict[str, Any]:
    """Run the install service dispatch for multiple targets in one pass."""
    return integrate_package_primitives(
        package_info,
        project_root,
        targets=[KNOWN_TARGETS[target_name] for target_name in target_names],
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


@pytest.mark.parametrize("target", ["claude", "copilot", "codex"])
def test_selected_target_does_not_activate_formerly_shadowed_generic_hooks(
    tmp_path: Path,
    target: str,
) -> None:
    """Neither a foreign descriptor nor the old fallback becomes newly executable."""
    project_root = tmp_path / "project"
    (project_root / ".claude").mkdir(parents=True)
    package_info = _make_ponytail_package(tmp_path)

    with pytest.raises(HookContractError, match="previously excluded or shadowed"):
        _integrate_package_hooks(package_info, project_root, target_name=target)
    assert not (project_root / ".claude/settings.json").exists()
    assert not (project_root / ".github/hooks").exists()
    assert not (project_root / ".codex/hooks.json").exists()


def test_combined_manifest_fails_before_any_selected_target_write(
    tmp_path: Path,
) -> None:
    """An incompatible multi-target selection must not partially activate hooks."""
    project_root = tmp_path / "project"
    (project_root / ".claude").mkdir(parents=True)
    (project_root / ".codex").mkdir(parents=True)
    package_info = _make_ponytail_package(tmp_path)

    with pytest.raises(HookContractError, match="previously excluded or shadowed"):
        _integrate_package_hooks_for_targets(package_info, project_root, ["claude", "codex"])
    assert not (project_root / ".claude/settings.json").exists()
    assert not (project_root / ".codex/hooks.json").exists()
