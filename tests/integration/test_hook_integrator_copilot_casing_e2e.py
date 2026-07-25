"""Hermetic install-path proof for Copilot hook event casing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apm_cli.core.scope import InstallScope
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


def _hook_entry(command: str) -> dict[str, Any]:
    """Return a hook matcher entry using the native nested hook shape."""
    return {"hooks": [{"type": "command", "command": command}]}


def _make_hook_package(
    tmp_path: Path,
    hooks: dict[str, list[dict[str, Any]]],
    *,
    name: str = "hook-casing-proof",
) -> PackageInfo:
    """Create a local package layout containing one .apm/hooks JSON file."""
    package_root = tmp_path / "apm_modules" / "local" / name
    hooks_dir = package_root / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps({"hooks": hooks}, indent=2),
        encoding="utf-8",
    )

    package = APMPackage(name=name, version="1.0.0", source=f"local/{name}")
    return PackageInfo(package=package, install_path=package_root)


def _integrate_package_hooks(
    package_info: PackageInfo,
    project_root: Path,
    *,
    target_name: str,
    user_scope: bool = False,
) -> dict[str, Any]:
    """Run the install service dispatch that invokes HookIntegrator for a target."""
    target = KNOWN_TARGETS[target_name].for_scope(user_scope=user_scope)
    return integrate_package_primitives(
        package_info,
        project_root,
        targets=[target],
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
        scope=InstallScope.USER if user_scope else InstallScope.PROJECT,
    )


def _read_copilot_hooks_config(project_root: Path, package_name: str) -> dict[str, Any]:
    """Read the actual Copilot hook JSON written under .github/hooks."""
    config_path = project_root / ".github" / "hooks" / f"{package_name}-hooks.json"
    assert config_path.exists()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["version"] == 1
    return config


def test_copilot_install_writes_only_camel_case_hook_events(tmp_path: Path) -> None:
    """Copilot hook integration rewrites Claude-style events to camelCase on disk."""
    project_root = tmp_path / "project"
    (project_root / ".github").mkdir(parents=True)
    (project_root / ".github" / "copilot-instructions.md").write_text(
        "# Copilot instructions\n",
        encoding="utf-8",
    )
    package_info = _make_hook_package(
        tmp_path,
        {
            "PreToolUse": [_hook_entry("echo pre")],
            "PostToolUse": [_hook_entry("echo post")],
            "UserPromptSubmit": [_hook_entry("echo prompt")],
            "Stop": [_hook_entry("echo stop")],
        },
    )

    result = _integrate_package_hooks(package_info, project_root, target_name="copilot")

    assert result["hooks"] == 1
    config = _read_copilot_hooks_config(project_root, package_info.package.name)
    hooks = config["hooks"]
    assert set(hooks) == {"preToolUse", "postToolUse", "userPromptSubmit", "stop"}
    assert {"PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"}.isdisjoint(hooks)


def test_copilot_install_merges_duplicate_event_aliases(tmp_path: Path) -> None:
    """Copilot hook integration unions PascalCase and camelCase alias entries."""
    project_root = tmp_path / "project"
    (project_root / ".github").mkdir(parents=True)
    (project_root / ".github" / "copilot-instructions.md").write_text(
        "# Copilot instructions\n",
        encoding="utf-8",
    )
    package_info = _make_hook_package(
        tmp_path,
        {
            "PreToolUse": [_hook_entry("echo pascal")],
            "preToolUse": [_hook_entry("echo camel")],
        },
    )

    result = _integrate_package_hooks(package_info, project_root, target_name="copilot")

    assert result["hooks"] == 1
    config = _read_copilot_hooks_config(project_root, package_info.package.name)
    hooks = config["hooks"]
    assert set(hooks) == {"preToolUse"}
    commands = [entry["hooks"][0]["command"] for entry in hooks["preToolUse"]]
    assert commands == ["echo pascal", "echo camel"]


@pytest.mark.parametrize("user_scope", [False, True])
def test_copilot_install_scope_controls_script_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    user_scope: bool,
) -> None:
    """Install dispatch keeps project paths relative and user paths absolute."""
    project_root = tmp_path / ("home" if user_scope else "project")
    project_root.mkdir(exist_ok=True)
    package_info = _make_hook_package(
        tmp_path,
        {"sessionStart": [_hook_entry("./run.sh")]},
        name=f"scope-{'user' if user_scope else 'project'}",
    )
    script = package_info.install_path / ".apm" / "hooks" / "run.sh"
    script.write_text("#!/bin/sh\necho scope\n", encoding="utf-8")
    target = KNOWN_TARGETS["copilot"].for_scope(user_scope=user_scope)

    result = _integrate_package_hooks(
        package_info,
        project_root,
        target_name="copilot",
        user_scope=user_scope,
    )

    assert result["hooks"] == 1
    config_path = (
        project_root / target.root_dir / "hooks" / f"{package_info.package.name}-hooks.json"
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    command = config["hooks"]["sessionStart"][0]["hooks"][0]["command"]
    installed_script = (
        project_root / target.root_dir / "hooks" / "scripts" / package_info.package.name / "run.sh"
    ).resolve()
    monkeypatch.chdir(package_info.install_path)
    if user_scope:
        assert Path(command).is_absolute()
        assert Path(command).resolve() == installed_script
    else:
        assert not Path(command).is_absolute()
        assert command == f"{target.root_dir}/hooks/scripts/{package_info.package.name}/run.sh"
        monkeypatch.chdir(project_root)
        assert Path(command).resolve() == installed_script


@pytest.mark.parametrize("user_scope", [False, True])
def test_kiro_install_scope_controls_script_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    user_scope: bool,
) -> None:
    """Kiro consumes the shared hook scope rewrite decision."""
    project_root = tmp_path / ("home" if user_scope else "project")
    (project_root / ".kiro").mkdir(parents=True)
    package_info = _make_hook_package(
        tmp_path,
        {"sessionStart": [_hook_entry("./run.sh")]},
        name=f"kiro-scope-{'user' if user_scope else 'project'}",
    )
    script = package_info.install_path / ".apm" / "hooks" / "run.sh"
    script.write_text("#!/bin/sh\necho scope\n", encoding="utf-8")
    target = KNOWN_TARGETS["kiro"].for_scope(user_scope=user_scope)

    result = _integrate_package_hooks(
        package_info,
        project_root,
        target_name="kiro",
        user_scope=user_scope,
    )

    assert result["hooks"] == 1
    hook_docs = sorted((project_root / target.root_dir / "hooks").glob("*.json"))
    assert len(hook_docs) == 1
    hook_doc = json.loads(hook_docs[0].read_text(encoding="utf-8"))
    command = hook_doc["hooks"][0]["action"]["command"]
    installed_script = (
        project_root / target.root_dir / "hooks" / package_info.package.name / "run.sh"
    ).resolve()
    monkeypatch.chdir(package_info.install_path)
    if user_scope:
        assert Path(command).is_absolute()
        assert Path(command).resolve() == installed_script
    else:
        assert not Path(command).is_absolute()
        assert command == f"{target.root_dir}/hooks/{package_info.package.name}/run.sh"
        monkeypatch.chdir(project_root)
        assert Path(command).resolve() == installed_script


def test_claude_install_preserves_pascal_case_hook_events(tmp_path: Path) -> None:
    """Claude hook integration keeps PascalCase events in settings.json."""
    project_root = tmp_path / "project"
    (project_root / ".claude").mkdir(parents=True)
    package_info = _make_hook_package(
        tmp_path,
        {
            "PreToolUse": [_hook_entry("echo pre")],
            "PostToolUse": [_hook_entry("echo post")],
            "Stop": [_hook_entry("echo stop")],
        },
    )

    result = _integrate_package_hooks(package_info, project_root, target_name="claude")

    assert result["hooks"] == 1
    settings = json.loads((project_root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == {"PreToolUse", "PostToolUse", "Stop"}
