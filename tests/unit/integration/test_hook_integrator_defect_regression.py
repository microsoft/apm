"""Regression tests for hook integrator defects (issues #1892, #1977)."""

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


# ---------------------------------------------------------------------------
# Issue #1977 -- Copilot hook events with PascalCase names must be renamed
# to camelCase during deployment (integrate_package_hooks).
# ---------------------------------------------------------------------------


def _pascal_hooks_json(command: str = "echo hook") -> dict:
    """Minimal hook JSON using PascalCase event names (Claude-authored style)."""
    return {
        "hooks": {
            "PreToolUse": [{"type": "command", "bash": command}],
            "PostToolUse": [{"type": "command", "bash": "echo post"}],
        }
    }


def test_copilot_pascal_events_renamed_to_camel(tmp_path: Path) -> None:
    """PascalCase events are silently renamed to camelCase on copilot deployment.

    Regression for issue #1977: _HOOK_EVENT_MAP had no 'copilot' entry and
    the copilot deploy path never applied any event renaming, so packages
    authored with Claude-style PascalCase event names were written verbatim
    and never recognised by Copilot.
    """
    project = tmp_path / "project"
    package_path = tmp_path / "pkg1977"
    (project / ".github").mkdir(parents=True)
    hooks_dir = package_path / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(_pascal_hooks_json()),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks(
        _package_info(package_path, name="pkg1977"),
        project,
    )

    output_file = project / ".github" / "hooks" / "pkg1977-hooks.json"
    assert output_file.exists(), "Output hook file was not created"
    data = json.loads(output_file.read_text(encoding="utf-8"))
    hook_keys = set(data.get("hooks", {}).keys())

    # camelCase keys must be present
    assert "preToolUse" in hook_keys, f"Expected 'preToolUse' in hook keys, got {hook_keys}"
    assert "postToolUse" in hook_keys, f"Expected 'postToolUse' in hook keys, got {hook_keys}"
    # PascalCase keys must not survive
    assert "PreToolUse" not in hook_keys, "PascalCase 'PreToolUse' was NOT renamed"
    assert "PostToolUse" not in hook_keys, "PascalCase 'PostToolUse' was NOT renamed"


def test_copilot_camel_events_pass_through_unchanged(tmp_path: Path) -> None:
    """Already-camelCase event names are written unchanged to the output file."""
    project = tmp_path / "project"
    package_path = tmp_path / "pkg-camel"
    (project / ".github").mkdir(parents=True)
    hooks_dir = package_path / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "preToolUse": [{"type": "command", "bash": "echo pre"}],
                    "postToolUse": [{"type": "command", "bash": "echo post"}],
                }
            }
        ),
        encoding="utf-8",
    )

    HookIntegrator().integrate_package_hooks(
        _package_info(package_path, name="pkg-camel"),
        project,
    )

    output_file = project / ".github" / "hooks" / "pkg-camel-hooks.json"
    assert output_file.exists()
    data = json.loads(output_file.read_text(encoding="utf-8"))
    hook_keys = set(data.get("hooks", {}).keys())

    assert hook_keys == {"preToolUse", "postToolUse"}
