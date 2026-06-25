"""Regression tests for hook integrator issue #1892 defects."""

from __future__ import annotations

import json
from pathlib import Path

from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.models.apm_package import APMPackage, PackageInfo


def _package_info(package_path: Path, name: str = "superpowers") -> PackageInfo:
    return PackageInfo(
        package=APMPackage(name=name, version="1.0.0", source=f"owner/{name}"),
        install_path=package_path,
    )


def _session_start_hook(command: str = "echo hook") -> dict:
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        }
    }


def test_same_hook_in_both_dirs_integrates_once(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package_path = tmp_path / "superpowers"
    (project / ".github").mkdir(parents=True)
    for hooks_dir in (package_path / ".apm" / "hooks", package_path / "hooks"):
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps(_session_start_hook()),
            encoding="utf-8",
        )

    hook_files = HookIntegrator().find_hook_files(package_path)
    result = HookIntegrator().integrate_package_hooks_claude(_package_info(package_path), project)

    assert [path.relative_to(package_path).as_posix() for path in hook_files] == [
        ".apm/hooks/hooks.json"
    ]
    assert result.files_integrated == 1


def test_overlapping_dirs_different_hooks_both_integrate(tmp_path: Path) -> None:
    package_path = tmp_path / "superpowers"
    (package_path / ".apm" / "hooks").mkdir(parents=True)
    (package_path / "hooks").mkdir(parents=True)
    (package_path / ".apm" / "hooks" / "first.json").write_text(
        json.dumps(_session_start_hook("echo first")),
        encoding="utf-8",
    )
    (package_path / "hooks" / "second.json").write_text(
        json.dumps(_session_start_hook("echo second")),
        encoding="utf-8",
    )

    hook_files = HookIntegrator().find_hook_files(package_path)

    assert [path.relative_to(package_path).as_posix() for path in hook_files] == [
        ".apm/hooks/first.json",
        "hooks/second.json",
    ]


def test_copilot_hook_path_no_doubling(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package_path = tmp_path / "superpowers"
    (project / ".github").mkdir(parents=True)
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "run-hook.cmd").write_text("@echo off\n", encoding="utf-8")
    (hooks_dir / "hooks-copilot.json").write_text(
        json.dumps({"hooks": {"sessionStart": [{"command": "./hooks/run-hook.cmd start"}]}}),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks(
        _package_info(package_path),
        project,
    )

    output = (project / ".github" / "hooks" / "superpowers-hooks-copilot.json").read_text(
        encoding="utf-8"
    )
    data = json.loads(output)
    command = data["hooks"]["sessionStart"][0]["command"]
    assert command == ".github/hooks/scripts/superpowers/hooks/run-hook.cmd start"
    assert (
        project / ".github" / "hooks" / "scripts" / "superpowers" / "hooks" / "run-hook.cmd"
    ).exists()


def test_claude_hook_script_path_no_doubling(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package_path = tmp_path / "superpowers"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "run-hook.cmd").write_text("@echo off\n", encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(_session_start_hook("./hooks/run-hook.cmd start")),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks_claude(_package_info(package_path), project)

    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == ".claude/hooks/superpowers/hooks/run-hook.cmd start"
    assert (project / ".claude" / "hooks" / "superpowers" / "hooks" / "run-hook.cmd").exists()
