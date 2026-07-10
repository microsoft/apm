"""Acceptance tests for the Kiro target profile and transforms (#702)."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.integration.hook_integrator import _HOOK_EVENT_MAP, HookIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    PackageType,
    ResolvedReference,
)


def _make_package_info(
    package_dir: Path,
    name: str = "test-pkg",
    package_type: PackageType | None = None,
) -> PackageInfo:
    package = APMPackage(
        name=name,
        version="1.0.0",
        package_path=package_dir,
        source=f"github.com/test/{name}",
    )
    resolved_ref = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )
    return PackageInfo(
        package=package,
        install_path=package_dir,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
        package_type=package_type,
    )


def test_kiro_is_discoverable_in_target_help() -> None:
    runner = CliRunner()

    install = runner.invoke(cli, ["install", "--help"])
    compile_result = runner.invoke(cli, ["compile", "--help"])

    expected_all_targets = "copilot+claude+cursor+opencode+codex+gemini+windsurf+kiro"
    install_help = "".join(install.output.split())
    compile_help = "".join(compile_result.output.split())

    assert install.exit_code == 0
    assert compile_result.exit_code == 0
    assert expected_all_targets in install_help
    assert expected_all_targets in compile_help
    assert "ClaudeCode" in install_help
    assert "Windsurf" in install_help


def test_kiro_runtime_discovered_in_user_scope_without_project_dir(tmp_path: Path) -> None:
    from apm_cli.integration.mcp_integrator_install import _discover_installed_runtimes

    assert not (tmp_path / ".kiro").exists()

    runtimes = _discover_installed_runtimes(tmp_path, user_scope=True)

    assert "kiro" in runtimes


def test_kiro_target_profile_matches_ratified_layout() -> None:
    target = KNOWN_TARGETS["kiro"]

    assert target.root_dir == ".kiro"
    assert target.auto_create is False
    assert target.detect_by_dir is True
    assert target.user_supported is True
    assert target.user_root_dir == ".kiro"
    assert set(target.primitives) == {"instructions", "skills", "hooks"}

    instructions = target.primitives["instructions"]
    assert instructions.subdir == "steering"
    assert instructions.extension == ".md"
    assert instructions.format_id == "kiro_steering"
    assert instructions.output_compare is True

    skills = target.primitives["skills"]
    assert skills.subdir == "skills"
    assert skills.extension == "/SKILL.md"
    assert skills.format_id == "skill_standard"

    hooks = target.primitives["hooks"]
    assert hooks.subdir == "hooks"
    assert hooks.extension == ".json"
    assert hooks.format_id == "kiro_hooks"


def test_kiro_v1_event_map_uses_canonical_trigger_names() -> None:
    event_map = _HOOK_EVENT_MAP["kiro"]

    assert {
        source: event_map[source]
        for source in (
            "preToolUse",
            "PostToolUse",
            "promptSubmit",
            "agentStop",
            "PreTaskExecution",
            "PostTaskExecution",
            "PostFileCreate",
            "PostFileSave",
            "PostFileDelete",
            "SessionStart",
        )
    } == {
        "preToolUse": "PreToolUse",
        "PostToolUse": "PostToolUse",
        "promptSubmit": "UserPromptSubmit",
        "agentStop": "Stop",
        "PreTaskExecution": "PreTaskExec",
        "PostTaskExecution": "PostTaskExec",
        "PostFileCreate": "PostFileCreate",
        "PostFileSave": "PostFileSave",
        "PostFileDelete": "PostFileDelete",
        "SessionStart": "SessionStart",
    }


def test_kiro_steering_maps_apply_to_to_file_match(tmp_path: Path) -> None:
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    instructions_dir = package_dir / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    (instructions_dir / "python.instructions.md").write_text(
        "---\n"
        "description: Python rules\n"
        'applyTo: "src/**/*.py,tests/**/*.py"\n'
        "---\n\n"
        "# Python\n\nUse type hints.\n",
        encoding="utf-8",
    )

    result = InstructionIntegrator().integrate_instructions_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 1
    target = tmp_path / ".kiro" / "steering" / "python.md"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == (
        "---\n"
        "inclusion: fileMatch\n"
        "fileMatchPattern:\n"
        '  - "src/**/*.py"\n'
        '  - "tests/**/*.py"\n'
        "---\n\n"
        "# Python\n\nUse type hints.\n"
    )


def test_kiro_steering_defaults_unscoped_instructions_to_always(tmp_path: Path) -> None:
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    instructions_dir = package_dir / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    (instructions_dir / "global.instructions.md").write_text(
        "# Global\n\nUse this everywhere.\n",
        encoding="utf-8",
    )

    result = InstructionIntegrator().integrate_instructions_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 1
    target = tmp_path / ".kiro" / "steering" / "global.md"
    assert target.read_text(encoding="utf-8") == (
        "---\ninclusion: always\n---\n\n# Global\n\nUse this everywhere.\n"
    )


def test_kiro_skills_deploy_skill_md_to_kiro_skills_dir(tmp_path: Path) -> None:
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "skill-pkg"
    package_dir.mkdir()
    (package_dir / "SKILL.md").write_text(
        "---\nname: skill-pkg\ndescription: Demo skill\n---\n\n# Demo\n",
        encoding="utf-8",
    )

    result = SkillIntegrator().integrate_package_skill(
        _make_package_info(package_dir, "skill-pkg", PackageType.CLAUDE_SKILL),
        tmp_path,
        targets=[KNOWN_TARGETS["kiro"]],
    )

    target = tmp_path / ".kiro" / "skills" / "skill-pkg" / "SKILL.md"
    assert result.skill_created is True
    assert target.read_text(encoding="utf-8") == (
        "---\nname: skill-pkg\ndescription: Demo skill\n---\n\n# Demo\n"
    )


def test_kiro_hooks_expand_each_apm_hook_to_individual_json(tmp_path: Path) -> None:
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "hookify"
    hooks_dir = package_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    hook_data = {
        "description": "Validate before tool use",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "fs_write|str_replace",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python ${PLUGIN_ROOT}/hooks/check.py",
                            "timeout": 10,
                        }
                    ],
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python ${PLUGIN_ROOT}/hooks/prompt.py",
                        }
                    ]
                }
            ],
        },
    }
    (hooks_dir / "hooks.json").write_text(json.dumps(hook_data), encoding="utf-8")
    (hooks_dir / "check.py").write_text("# check\n", encoding="utf-8")
    (hooks_dir / "prompt.py").write_text("# prompt\n", encoding="utf-8")

    result = HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir, "hookify"),
        tmp_path,
    )

    assert result.files_integrated == 2
    assert result.scripts_copied == 2

    pre_tool = tmp_path / ".kiro" / "hooks" / "hookify-hooks-pretooluse-1.json"
    prompt_submit = tmp_path / ".kiro" / "hooks" / "hookify-hooks-userpromptsubmit-1.json"
    assert pre_tool.exists()
    assert prompt_submit.exists()

    pre_data = json.loads(pre_tool.read_text(encoding="utf-8"))
    assert pre_data == {
        "version": "v1",
        "hooks": [
            {
                "name": "hookify PreToolUse 1",
                "trigger": "PreToolUse",
                "matcher": "fs_write|str_replace",
                "action": {
                    "type": "command",
                    "command": "python .kiro/hooks/hookify/hooks/check.py",
                },
                "timeout": 10,
            }
        ],
    }
    if sys.platform != "win32":
        assert pre_tool.stat().st_mode & 0o777 == 0o600

    prompt_data = json.loads(prompt_submit.read_text(encoding="utf-8"))
    assert prompt_data == {
        "version": "v1",
        "hooks": [
            {
                "name": "hookify UserPromptSubmit 1",
                "trigger": "UserPromptSubmit",
                "action": {
                    "type": "command",
                    "command": "python .kiro/hooks/hookify/hooks/prompt.py",
                },
            }
        ],
    }
    if sys.platform != "win32":
        assert prompt_submit.stat().st_mode & 0o777 == 0o600

    assert (tmp_path / ".kiro" / "hooks" / "hookify" / "hooks" / "check.py").exists()
    assert (tmp_path / ".kiro" / "hooks" / "hookify" / "hooks" / "prompt.py").exists()


def test_kiro_deploys_hook_directory_siblings_and_package_module_type(
    tmp_path: Path,
) -> None:
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "ponytail"
    hooks_dir = package_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (package_dir / "package.json").write_text(
        json.dumps({"type": "commonjs"}),
        encoding="utf-8",
    )
    (hooks_dir / "ponytail.js").write_text(
        "const config = require('./ponytail-config');\nconsole.log(config.message);\n",
        encoding="utf-8",
    )
    (hooks_dir / "ponytail-config.js").write_text(
        "module.exports = { message: 'ok' };\n",
        encoding="utf-8",
    )
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "node ${PLUGIN_ROOT}/hooks/ponytail.js",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir, "ponytail"),
        tmp_path,
    )

    deployed_root = tmp_path / ".kiro" / "hooks" / "ponytail" / "hooks"
    deployed_script = deployed_root / "ponytail.js"
    deployed_sibling = deployed_root / "ponytail-config.js"
    deployed_package_json = deployed_root / "package.json"
    assert result.files_integrated == 1
    assert result.scripts_copied == 1
    assert deployed_script.exists()
    assert deployed_sibling.exists()
    assert json.loads(deployed_package_json.read_text(encoding="utf-8")) == {"type": "commonjs"}
    assert deployed_script in result.target_paths
    assert deployed_sibling in result.target_paths
    assert deployed_package_json in result.target_paths


def test_kiro_hooks_convert_prompt_actions_to_ask_agent(tmp_path: Path) -> None:
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "prompt-hooks"
    hooks_dir = package_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    hook_data = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "askAgent",
                            "prompt": "Review the submitted prompt for policy drift.",
                        }
                    ]
                }
            ]
        }
    }
    (hooks_dir / "hooks.json").write_text(json.dumps(hook_data), encoding="utf-8")

    result = HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir, "prompt-hooks"),
        tmp_path,
    )

    target = tmp_path / ".kiro" / "hooks" / "prompt-hooks-hooks-userpromptsubmit-1.json"
    assert result.files_integrated == 1
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {
        "version": "v1",
        "hooks": [
            {
                "name": "prompt-hooks UserPromptSubmit 1",
                "trigger": "UserPromptSubmit",
                "action": {
                    "type": "agent",
                    "prompt": "Review the submitted prompt for policy drift.",
                },
            }
        ],
    }
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600


def test_kiro_hooks_skip_when_project_has_no_kiro_dir(tmp_path: Path) -> None:
    package_dir = tmp_path / "hookify"
    hooks_dir = package_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": "echo hi"}]}]}}),
        encoding="utf-8",
    )

    result = HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir, "hookify"),
        tmp_path,
    )

    assert result.files_integrated == 0
    assert not (tmp_path / ".kiro").exists()


def test_kiro_hooks_accept_native_v1_documents(tmp_path: Path) -> None:
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "native-hooks"
    hooks_dir = package_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "native.json").write_text(
        json.dumps(
            {
                "version": "v1",
                "hooks": [
                    {
                        "name": "check task",
                        "description": "Validate the task before execution",
                        "trigger": "PreTaskExec",
                        "matcher": "build",
                        "timeout": 15,
                        "enabled": False,
                        "action": {
                            "type": "command",
                            "command": "python ${PLUGIN_ROOT}/hooks/check.py",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (hooks_dir / "check.py").write_text("# check\n", encoding="utf-8")

    result = HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir, "native-hooks"),
        tmp_path,
    )

    target = tmp_path / ".kiro" / "hooks" / "native-hooks-native-pretaskexec-1.json"
    assert result.files_integrated == 1
    assert result.scripts_copied == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "version": "v1",
        "hooks": [
            {
                "name": "check task",
                "description": "Validate the task before execution",
                "trigger": "PreTaskExec",
                "matcher": "build",
                "timeout": 15,
                "enabled": False,
                "action": {
                    "type": "command",
                    "command": "python .kiro/hooks/native-hooks/hooks/check.py",
                },
            }
        ],
    }
