"""Regression tests for hook integrator issue #1892 defects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_copilot_renames_claude_tool_events_to_camel_case(tmp_path: Path, caplog, capsys) -> None:
    project = tmp_path / "project"
    package_path = tmp_path / "superpowers"
    hooks_dir = package_path / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [{"hooks": [{"type": "command", "command": "echo pre"}]}],
                    "PostToolUse": [{"hooks": [{"type": "command", "command": "echo post"}]}],
                }
            }
        ),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks(_package_info(package_path), project)

    output = json.loads(
        (project / ".github" / "hooks" / "superpowers-hooks.json").read_text(encoding="utf-8")
    )
    assert set(output["hooks"]) == {"preToolUse", "postToolUse"}
    captured = capsys.readouterr()
    assert "may not be recognized" not in captured.out
    assert "hook event casing mismatch" not in caplog.text


def test_copilot_merges_duplicate_event_aliases_to_camel_case(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package_path = tmp_path / "superpowers"
    hooks_dir = package_path / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [{"hooks": [{"type": "command", "command": "echo pascal"}]}],
                    "preToolUse": [{"hooks": [{"type": "command", "command": "echo camel"}]}],
                }
            }
        ),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks(_package_info(package_path), project)

    output = json.loads(
        (project / ".github" / "hooks" / "superpowers-hooks.json").read_text(encoding="utf-8")
    )
    assert set(output["hooks"]) == {"preToolUse"}
    commands = [entry["hooks"][0]["command"] for entry in output["hooks"]["preToolUse"]]
    assert commands == ["echo pascal", "echo camel"]


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


def _setup_commonjs_hook_package(
    tmp_path: Path, target_manifest: str = "hooks.json"
) -> PackageInfo:
    package_path = tmp_path / "ponytail"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "lib").mkdir()
    (package_path / "package.json").write_text(
        json.dumps({"type": "commonjs"}),
        encoding="utf-8",
    )
    (hooks_dir / "ponytail.js").write_text(
        "const config = require('./ponytail-config');\n"
        "const helper = require('./lib/helper');\n"
        "console.log(config.message, helper.message);\n",
        encoding="utf-8",
    )
    (hooks_dir / "ponytail-config.js").write_text(
        "module.exports = { message: 'ok' };\n",
        encoding="utf-8",
    )
    (hooks_dir / "lib" / "helper.js").write_text(
        "module.exports = { message: 'nested' };\n",
        encoding="utf-8",
    )
    (hooks_dir / target_manifest).write_text(
        json.dumps(_session_start_hook("${CLAUDE_PLUGIN_ROOT}/hooks/ponytail.js session-start")),
        encoding="utf-8",
    )
    return _package_info(package_path, "ponytail")


def test_claude_deploys_hook_directory_siblings_and_package_module_type(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text(json.dumps({"type": "module"}), encoding="utf-8")
    pkg_info = _setup_commonjs_hook_package(tmp_path)

    result = HookIntegrator().integrate_package_hooks_claude(pkg_info, project)

    deployed_script = project / ".claude" / "hooks" / "ponytail" / "hooks" / "ponytail.js"
    deployed_package_json = deployed_script.parent / "package.json"
    assert deployed_script.exists()
    assert (deployed_script.parent / "ponytail-config.js").exists()
    assert (deployed_script.parent / "lib" / "helper.js").exists()
    assert json.loads(deployed_package_json.read_text(encoding="utf-8")) == {"type": "commonjs"}
    assert deployed_script in result.target_paths
    assert (deployed_script.parent / "ponytail-config.js") in result.target_paths
    assert (deployed_script.parent / "lib" / "helper.js") in result.target_paths
    assert deployed_package_json in result.target_paths


def test_copilot_deploys_hook_directory_siblings_and_package_module_type(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text(json.dumps({"type": "module"}), encoding="utf-8")
    pkg_info = _setup_commonjs_hook_package(tmp_path, "hooks-copilot.json")

    result = HookIntegrator().integrate_package_hooks(pkg_info, project)

    deployed_script = (
        project / ".github" / "hooks" / "scripts" / "ponytail" / "hooks" / "ponytail.js"
    )
    deployed_package_json = deployed_script.parent / "package.json"
    assert deployed_script.exists()
    assert (deployed_script.parent / "ponytail-config.js").exists()
    assert (deployed_script.parent / "lib" / "helper.js").exists()
    assert json.loads(deployed_package_json.read_text(encoding="utf-8")) == {"type": "commonjs"}
    assert deployed_script in result.target_paths
    assert (deployed_script.parent / "ponytail-config.js") in result.target_paths
    assert (deployed_script.parent / "lib" / "helper.js") in result.target_paths
    assert deployed_package_json in result.target_paths


def test_hook_bundle_defaults_sidecar_to_commonjs_without_package_type(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    package_path = tmp_path / "defaulttype"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "entry.js").write_text("console.log('ok');\n", encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(_session_start_hook("${CLAUDE_PLUGIN_ROOT}/hooks/entry.js")),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks_claude(
        _package_info(package_path, "defaulttype"), project
    )

    deployed_package_json = project / ".claude" / "hooks" / "defaulttype" / "hooks" / "package.json"
    assert json.loads(deployed_package_json.read_text(encoding="utf-8")) == {"type": "commonjs"}


def test_hook_bundle_does_not_deploy_root_hook_descriptor_manifest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pkg_info = _setup_commonjs_hook_package(tmp_path)

    HookIntegrator().integrate_package_hooks_claude(pkg_info, project)

    deployed_root = project / ".claude" / "hooks" / "ponytail" / "hooks"
    assert not (deployed_root / "hooks.json").exists()


def test_hook_bundle_skips_package_sidecar_for_shell_only_hooks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    package_path = tmp_path / "shellhooks"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "run.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(_session_start_hook("${CLAUDE_PLUGIN_ROOT}/hooks/run.sh")),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks_claude(
        _package_info(package_path, "shellhooks"), project
    )

    deployed_root = project / ".claude" / "hooks" / "shellhooks" / "hooks"
    assert (deployed_root / "run.sh").exists()
    assert not (deployed_root / "package.json").exists()


def test_hook_bundle_excludes_symlinks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    package_path = tmp_path / "linkhooks"
    hooks_dir = package_path / "hooks"
    hooks_dir.mkdir(parents=True)
    outside_file = tmp_path / "outside-secret.js"
    outside_file.write_text("module.exports = 'secret';\n", encoding="utf-8")
    (hooks_dir / "entry.js").write_text("require('./linked-secret');\n", encoding="utf-8")
    try:
        (hooks_dir / "linked-secret.js").symlink_to(outside_file)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(_session_start_hook("${CLAUDE_PLUGIN_ROOT}/hooks/entry.js")),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks_claude(
        _package_info(package_path, "linkhooks"), project
    )

    deployed_root = project / ".claude" / "hooks" / "linkhooks" / "hooks"
    assert (deployed_root / "entry.js").exists()
    assert not (deployed_root / "linked-secret.js").exists()


def test_hook_bundle_stale_sibling_removed_from_target_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pkg_info = _setup_commonjs_hook_package(tmp_path)
    integrator = HookIntegrator()

    result = integrator.integrate_package_hooks_claude(pkg_info, project)
    stale_sibling = project / ".claude" / "hooks" / "ponytail" / "hooks" / "ponytail-config.js"
    assert stale_sibling in result.target_paths
    managed_files = {path.relative_to(project).as_posix() for path in result.target_paths}

    sync_result = integrator.sync_integration(None, project, managed_files=managed_files)

    assert sync_result["files_removed"] >= 4
    assert not stale_sibling.exists()
