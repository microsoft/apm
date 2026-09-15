"""Acceptance tests for the IBM Bob target (#2736)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from apm_cli.core.target_detection import detect_target, should_compile_agents_md
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS, active_targets
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    PackageType,
    ResolvedReference,
)


def _package_info(
    package_dir: Path,
    name: str = "bob-package",
    package_type: PackageType | None = None,
) -> PackageInfo:
    package = APMPackage(
        name=name,
        version="1.0.0",
        package_path=package_dir,
        source=f"github.com/test/{name}",
    )
    resolved = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )
    return PackageInfo(
        package=package,
        install_path=package_dir,
        resolved_reference=resolved,
        installed_at=datetime.now().isoformat(),
        package_type=package_type,
    )


def _hook_package(tmp_path: Path) -> PackageInfo:
    package_dir = tmp_path / "package"
    hooks_dir = package_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "check.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "preToolUse": [
                        {
                            "matcher": "^write_file$",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "sh ${PLUGIN_ROOT}/hooks/check.sh",
                                    "timeout": 5,
                                }
                            ],
                        }
                    ],
                    "PreTaskExecution": [{"command": "echo unsupported"}],
                }
            }
        ),
        encoding="utf-8",
    )
    return _package_info(package_dir)


def test_bob_profile_and_detection_match_native_contract(tmp_path: Path) -> None:
    target = KNOWN_TARGETS["bob"]
    assert target.root_dir == ".bob"
    assert target.auto_create is False
    assert target.detect_by_dir is True
    assert target.user_supported is True
    assert target.compile_family == "agents"
    assert set(target.primitives) == {"skills", "hooks"}
    assert target.primitives["skills"].extension == "/SKILL.md"
    assert target.hooks_config_display == ".bob/settings.json"

    (tmp_path / ".bob").mkdir()
    assert detect_target(tmp_path) == ("bob", "detected .bob/ folder")
    assert [profile.name for profile in active_targets(tmp_path)] == ["bob"]
    assert should_compile_agents_md("bob") is True


def test_bob_skill_deploys_to_project_skill_directory(tmp_path: Path) -> None:
    (tmp_path / ".bob").mkdir()
    package_dir = tmp_path / "bob-skill"
    package_dir.mkdir()
    (package_dir / "SKILL.md").write_text(
        "---\nname: bob-skill\ndescription: Bob skill\n---\n\n# Bob skill\n",
        encoding="utf-8",
    )

    result = SkillIntegrator().integrate_package_skill(
        _package_info(package_dir, "bob-skill", PackageType.CLAUDE_SKILL),
        tmp_path,
        targets=[KNOWN_TARGETS["bob"]],
    )

    deployed = tmp_path / ".bob" / "skills" / "bob-skill" / "SKILL.md"
    assert result.skill_created is True
    assert deployed.is_file()

    user_home = tmp_path / "home"
    (user_home / ".bob").mkdir(parents=True)
    user_target = KNOWN_TARGETS["bob"].for_scope(user_scope=True)
    assert user_target is not None
    user_result = SkillIntegrator().integrate_package_skill(
        _package_info(package_dir, "bob-skill", PackageType.CLAUDE_SKILL),
        user_home,
        targets=[user_target],
    )
    assert user_result.skill_created is True
    assert (user_home / ".bob" / "skills" / "bob-skill" / "SKILL.md").is_file()


def test_bob_hooks_merge_into_project_settings_and_preserve_user_entries(
    tmp_path: Path,
) -> None:
    bob_dir = tmp_path / ".bob"
    bob_dir.mkdir()
    settings = bob_dir / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "userSetting": True,
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo user"}]}]},
            }
        ),
        encoding="utf-8",
    )

    result = HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["bob"],
        _hook_package(tmp_path),
        tmp_path,
    )

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert result.files_integrated == 1
    assert data["userSetting"] is True
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo user"
    entry = data["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "^write_file$"
    assert entry["hooks"][0]["command"] == "sh .bob/hooks/package/hooks/check.sh"
    assert "_apm_source" not in entry
    assert "PreTaskExecution" not in data["hooks"]
    assert (bob_dir / "hooks" / "package" / "hooks" / "check.sh").is_file()
    sidecar = json.loads((bob_dir / "apm-hooks.json").read_text(encoding="utf-8"))
    assert sidecar["PreToolUse"][0]["_apm_source"] == "package"


def test_bob_hooks_use_documented_user_settings_path(tmp_path: Path) -> None:
    target = KNOWN_TARGETS["bob"].for_scope(user_scope=True)
    assert target is not None

    result = HookIntegrator().integrate_hooks_for_target(
        target,
        _hook_package(tmp_path),
        tmp_path,
        user_scope=True,
    )

    settings = tmp_path / ".bob" / "settings" / "settings.json"
    assert result.files_integrated == 1
    assert settings.is_file()
    assert (tmp_path / ".bob" / "settings" / "apm-hooks.json").is_file()
    assert (tmp_path / ".bob" / "hooks" / "package" / "hooks" / "check.sh").is_file()

    cleanup = HookIntegrator().sync_integration(
        None,
        tmp_path,
        managed_files=set(),
        targets=[target],
    )
    assert cleanup["errors"] == 0
    assert "hooks" not in json.loads(settings.read_text(encoding="utf-8"))
    assert not (tmp_path / ".bob" / "settings" / "apm-hooks.json").exists()


def test_bob_dropped_user_target_cleans_nested_settings_path(tmp_path: Path) -> None:
    target = KNOWN_TARGETS["bob"].for_scope(user_scope=True)
    assert target is not None

    HookIntegrator().integrate_hooks_for_target(
        target,
        _hook_package(tmp_path),
        tmp_path,
        user_scope=True,
    )

    settings = tmp_path / ".bob" / "settings" / "settings.json"
    assert settings.is_file()

    cleanup = HookIntegrator().reconcile_dropped_targets(
        tmp_path,
        {"bob"},
        user_scope=True,
    )

    assert cleanup["errors"] == 0
    assert "hooks" not in json.loads(settings.read_text(encoding="utf-8"))
    assert not (tmp_path / ".bob" / "settings" / "apm-hooks.json").exists()
