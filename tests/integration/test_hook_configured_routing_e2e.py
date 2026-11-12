"""Hermetic install-path proof for configuration-led hook routing (#3226).

The ponytail layout below relied on retired filename routing (a universal
Copilot-shaped ``hooks.json`` beside ``claude-codex-hooks.json`` and
``copilot-hooks.json``). It now fails with migration guidance before any hook
write. The migrated layout splits per-target hooks into packages that declare
``target:`` in their own manifest, and every lifecycle step follows that
configuration intersected with the active targets.
"""

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

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any accidental home-scoped writes inside the pytest temp tree."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


_COPILOT_SHAPE_HOOKS = {
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


def _write(path: Path, hooks: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}, indent=2), encoding="utf-8")


def _package(root: Path, name: str, target: Any = None) -> PackageInfo:
    package = APMPackage(name=name, version="1.0.0", source=f"DietrichGebert/{name}")
    if target is not None:
        package.target = target
    return PackageInfo(package=package, install_path=root)


def _legacy_ponytail_package(tmp_path: Path) -> PackageInfo:
    root = tmp_path / "apm_modules" / "local" / "ponytail"
    _write(root / ".apm" / "hooks" / "hooks.json", _COPILOT_SHAPE_HOOKS)
    _write(root / "hooks" / "claude-codex-hooks.json", _CLAUDE_SHAPE_HOOKS)
    _write(root / "hooks" / "copilot-hooks.json", _COPILOT_SHAPE_HOOKS)
    return _package(root, "ponytail")


def _migrated_packages(tmp_path: Path) -> tuple[PackageInfo, PackageInfo]:
    modules = tmp_path / "apm_modules" / "local"
    claude_root = modules / "ponytail-claude"
    _write(claude_root / "hooks" / "hooks.json", _CLAUDE_SHAPE_HOOKS)
    copilot_root = modules / "ponytail-copilot"
    _write(copilot_root / "hooks" / "hooks.json", _COPILOT_SHAPE_HOOKS)
    return (
        _package(claude_root, "ponytail-claude", ["claude", "codex"]),
        _package(copilot_root, "ponytail-copilot", "copilot"),
    )


def _install(
    package_info: PackageInfo,
    project_root: Path,
    target_names: list[str],
    *,
    dep_target_subset: list[str] | None = None,
) -> dict[str, Any]:
    """Run the install service dispatch that invokes HookIntegrator."""
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
        dep_target_subset=dep_target_subset,
    )


def _project(tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    for directory in (".claude", ".codex", ".github"):
        (project_root / directory).mkdir(parents=True)
    return project_root


def _hooks(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")).get("hooks", {})


@pytest.mark.parametrize("target_name", ["claude", "codex", "copilot"])
def test_legacy_filename_layout_fails_before_any_hook_write(
    tmp_path: Path, target_name: str
) -> None:
    project_root = _project(tmp_path)

    with pytest.raises(HookContractError) as excinfo:
        _install(_legacy_ponytail_package(tmp_path), project_root, [target_name])

    assert f"Target '{target_name}' would newly activate" in str(excinfo.value)
    assert not (project_root / ".claude" / "settings.json").exists()
    assert not (project_root / ".codex" / "hooks.json").exists()
    assert not (project_root / ".github" / "hooks").exists()


def test_migrated_packages_follow_their_configured_targets(tmp_path: Path) -> None:
    project_root = _project(tmp_path)
    claude_pkg, copilot_pkg = _migrated_packages(tmp_path)
    targets = ["claude", "codex", "copilot"]

    assert _install(claude_pkg, project_root, targets)["hooks"] == 2
    assert _install(copilot_pkg, project_root, targets)["hooks"] == 1
    # A repeat install is idempotent: no duplicated registrations or files.
    _install(claude_pkg, project_root, targets)
    _install(copilot_pkg, project_root, targets)

    claude = _hooks(project_root / ".claude" / "settings.json")
    assert set(claude) == {"SessionStart", "SubagentStart"}
    assert all(len(entries) == 1 for entries in claude.values())
    raw = json.dumps(claude)
    for copilot_field in ("sessionStart", "bash", "powershell", "timeoutSec"):
        assert copilot_field not in raw
    assert set(_hooks(project_root / ".codex" / "hooks.json")) == {"SessionStart", "SubagentStart"}
    copilot_files = sorted(p.name for p in (project_root / ".github" / "hooks").glob("*.json"))
    assert copilot_files == ["ponytail-copilot-hooks.json"]


def test_consumer_target_contraction_retires_the_dropped_target(tmp_path: Path) -> None:
    project_root = _project(tmp_path)
    claude_pkg, _copilot_pkg = _migrated_packages(tmp_path)
    targets = ["claude", "codex"]
    _install(claude_pkg, project_root, targets)

    result = _install(claude_pkg, project_root, targets, dep_target_subset=["codex"])

    assert result["hooks"] == 1
    claude_settings = project_root / ".claude" / "settings.json"
    assert not claude_settings.exists() or not _hooks(claude_settings)
    assert set(_hooks(project_root / ".codex" / "hooks.json")) == {"SessionStart", "SubagentStart"}
