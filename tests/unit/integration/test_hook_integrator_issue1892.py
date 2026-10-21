"""Regression tests for issue #1892 hook manifest routing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.hook_contract import HookContractError
from apm_cli.integration.hook_integrator import HookIntegrator, _filter_hook_files_for_target
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo

pytestmark = pytest.mark.component


def _make_package_info(install_path: Path, name: str = "superpowers") -> PackageInfo:
    package = APMPackage(name=name, version="1.0.0")
    return PackageInfo(package=package, install_path=install_path)


def _session_start_hook(command: str) -> dict:
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear",
                    "hooks": [{"type": "command", "command": command, "async": False}],
                }
            ]
        }
    }


@pytest.mark.parametrize("target", ["claude", "codex", "cursor"])
def test_migration_detects_formerly_shadowed_or_foreign_sources(
    tmp_path: Path,
    target: str,
) -> None:
    files = [
        tmp_path / "hooks.json",
        tmp_path / "hooks-codex.json",
        tmp_path / "codex-hooks.json",
        tmp_path / "hooks-cursor.json",
        tmp_path / "cursor-hooks.json",
    ]

    with pytest.raises(HookContractError, match="previously excluded or shadowed"):
        _filter_hook_files_for_target(files, target)


def test_claude_and_codex_do_not_merge_foreign_or_base_manifests(
    tmp_path: Path,
    capsys,
) -> None:
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True)
    pkg_dir = project / "apm_modules" / "superpowers"
    hooks_dir = pkg_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "run-hook.cmd").write_text("@echo off\n", encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(_session_start_hook("${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd session-start")),
        encoding="utf-8",
    )
    (hooks_dir / "hooks-codex.json").write_text(
        json.dumps(_session_start_hook("${PLUGIN_ROOT}/hooks/run-hook.cmd session-start-codex")),
        encoding="utf-8",
    )
    (hooks_dir / "hooks-cursor.json").write_text(
        json.dumps(
            {"hooks": {"sessionStart": [{"command": "./hooks/run-hook.cmd session-start"}]}}
        ),
        encoding="utf-8",
    )
    pkg_info = _make_package_info(pkg_dir)

    integrator = HookIntegrator()
    with pytest.raises(HookContractError, match="previously excluded or shadowed"):
        integrator.integrate_package_hooks_claude(pkg_info, project)
    with pytest.raises(HookContractError, match="previously excluded or shadowed"):
        integrator.integrate_package_hooks_codex(pkg_info, project)
    captured = capsys.readouterr()

    assert "sessionStart" not in captured.out
    assert "may not be recognized" not in captured.out
    assert not (project / ".claude/settings.json").exists()
    assert not (project / ".codex/hooks.json").exists()


def test_mirrored_manifests_integrate_once_per_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    pkg_dir = project / "apm_modules" / "superpowers"
    for hooks_dir in (pkg_dir / ".apm" / "hooks", pkg_dir / "hooks"):
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps(
                _session_start_hook("${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd session-start")
            ),
            encoding="utf-8",
        )
    (pkg_dir / "hooks" / "run-hook.cmd").write_text("@echo off\n", encoding="utf-8")
    pkg_info = _make_package_info(pkg_dir)

    result = HookIntegrator().integrate_package_hooks_claude(pkg_info, project)

    assert result.files_integrated == 1
    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert len(settings["hooks"]["SessionStart"]) == 1


def test_relative_hooks_path_from_hooks_dir_uses_package_root(
    tmp_path: Path,
    capsys,
) -> None:
    project = tmp_path / "project"
    (project / ".cursor").mkdir(parents=True)
    pkg_dir = project / "apm_modules" / "superpowers"
    hooks_dir = pkg_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "run-hook.cmd").write_text("@echo off\n", encoding="utf-8")
    (hooks_dir / "hooks-cursor.json").write_text(
        json.dumps(
            {"hooks": {"sessionStart": [{"command": "./hooks/run-hook.cmd session-start"}]}}
        ),
        encoding="utf-8",
    )
    pkg_info = _make_package_info(pkg_dir)

    result = HookIntegrator().integrate_hooks_for_target(KNOWN_TARGETS["cursor"], pkg_info, project)
    captured = capsys.readouterr()

    assert "Hook script not found" not in captured.out
    assert result.scripts_copied == 1
    copied = project / ".cursor" / "hooks" / "superpowers" / "hooks" / "run-hook.cmd"
    assert copied.exists()
    hooks_json = json.loads((project / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
    assert hooks_json["hooks"]["sessionStart"][0]["command"] == (
        ".cursor/hooks/superpowers/hooks/run-hook.cmd session-start"
    )
