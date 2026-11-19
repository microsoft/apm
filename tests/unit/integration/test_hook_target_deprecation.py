"""Retired filename-suffix hook target routing (#3226)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.hook_contract import HookContractError
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.models.apm_package import APMPackage, PackageInfo


def _package_info(package_path: Path, name: str = "superpowers") -> PackageInfo:
    return PackageInfo(
        package=APMPackage(name=name, version="1.0.0", source=f"owner/{name}"),
        install_path=package_path,
    )


def _hook(command: str) -> dict:
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


def _codex_named_package(tmp_path: Path) -> Path:
    package_path = tmp_path / "superpowers"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks-codex.json").write_text(json.dumps(_hook("echo codex")), encoding="utf-8")
    return package_path


def test_matching_target_installs_without_deprecation_warning(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True)

    result = HookIntegrator().integrate_package_hooks_codex(
        _package_info(_codex_named_package(tmp_path)), project
    )

    assert result.files_integrated == 1
    settings = json.loads((project / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == {"SessionStart"}
    assert "deprecated" not in capsys.readouterr().out


def test_other_target_fails_instead_of_gaining_the_hook(tmp_path: Path) -> None:
    project = tmp_path / "project"

    with pytest.raises(HookContractError) as excinfo:
        HookIntegrator().integrate_package_hooks_claude(
            _package_info(_codex_named_package(tmp_path)), project
        )

    message = str(excinfo.value)
    assert "owner/superpowers: hook filename target routing was removed" in message
    assert "Target 'claude' would newly activate: hooks/hooks-codex.json" in message
    assert not (project / ".claude" / "settings.json").exists()
