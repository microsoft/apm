"""Tests for deprecated filename-suffix hook target routing."""

from __future__ import annotations

import json
from pathlib import Path

from apm_cli.integration.hook_file_routing import _deprecated_filename_routing_warning
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


def test_target_suffix_file_emits_deprecation_warning(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    package_path = tmp_path / "superpowers"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks-codex.json").write_text(
        json.dumps(_hook("echo codex")),
        encoding="utf-8",
    )
    (project / ".codex").mkdir(parents=True)

    HookIntegrator().integrate_package_hooks_codex(_package_info(package_path), project)
    output = capsys.readouterr().out

    assert "filename-based target routing is deprecated" in output
    assert "'hooks-codex.json' routes via suffix to [codex]" in output
    assert "Update your apm.yml dependency to object form:" in output
    assert "targets: [codex]" in output


def test_target_suffix_file_not_universalized(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package_path = tmp_path / "superpowers"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks-codex.json").write_text(
        json.dumps(_hook("echo codex")),
        encoding="utf-8",
    )

    result = HookIntegrator().integrate_package_hooks_claude(_package_info(package_path), project)

    assert result.files_integrated == 0
    assert not (project / ".claude" / "settings.json").exists()


def test_target_suffix_file_still_works_for_matching_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True)
    package_path = tmp_path / "superpowers"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks-codex.json").write_text(
        json.dumps(_hook("echo codex")),
        encoding="utf-8",
    )

    result = HookIntegrator().integrate_package_hooks_codex(_package_info(package_path), project)

    assert result.files_integrated == 1
    settings = json.loads((project / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == {"SessionStart"}


def test_deprecation_warning_uses_placeholder_when_identity_unknown() -> None:
    """Migration snippets never present 'unknown' as copy-paste git identity."""
    warning = _deprecated_filename_routing_warning("", "", "hooks-codex.json", ["codex"])

    assert "git: <owner/repo>" in warning
    assert "git: unknown" not in warning
