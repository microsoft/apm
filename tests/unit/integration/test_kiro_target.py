"""Acceptance tests for the Kiro target profile and transforms (#702, #2089)."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from apm_cli.integration.agent_integrator import KIRO_AGENT_ALLOWED_TOOLS, AgentIntegrator
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    PackageType,
    ResolvedReference,
)
from apm_cli.utils.diagnostics import CATEGORY_ERROR, CATEGORY_WARNING, DiagnosticCollector


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
    assert set(target.primitives) == {"agents", "instructions", "prompts", "skills", "hooks"}

    agents = target.primitives["agents"]
    assert agents.subdir == "agents"
    assert agents.extension == ".md"
    assert agents.format_id == "kiro_agent"
    assert agents.output_compare is False

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
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python ${PLUGIN_ROOT}/hooks/check.py",
                        }
                    ]
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
                "action": {
                    "type": "command",
                    "command": "python .kiro/hooks/hookify/hooks/check.py",
                },
            }
        ],
    }
    if sys.platform != "win32":
        assert pre_tool.stat().st_mode & 0o777 == 0o600

    prompt_data = json.loads(prompt_submit.read_text(encoding="utf-8"))
    assert prompt_data["hooks"][0]["trigger"] == "UserPromptSubmit"
    assert prompt_data["hooks"][0]["action"]["command"] == (
        "python .kiro/hooks/hookify/hooks/prompt.py"
    )
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


def test_kiro_hooks_convert_prompt_actions_to_v1_agent(tmp_path: Path) -> None:
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


# ---------------------------------------------------------------------------
# Kiro agents (#2089) - .kiro/agents/<relative-stem>.md
# ---------------------------------------------------------------------------


def test_kiro_agents_deploy_plain_body_no_frontmatter(tmp_path: Path) -> None:
    """Agent with no frontmatter: body preserved verbatim as .md."""
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    (apm_agents / "reviewer.agent.md").write_text(
        "# Reviewer\n\nYou review code.\n",
        encoding="utf-8",
    )

    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 1
    target = tmp_path / ".kiro" / "agents" / "reviewer.md"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "# Reviewer\n\nYou review code.\n"


def test_kiro_agents_preserve_description_model_tools(tmp_path: Path) -> None:
    """description, model, and tools are preserved in the target frontmatter."""
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    (apm_agents / "helper.agent.md").write_text(
        "---\n"
        "description: My helpful agent\n"
        "model: claude-3-5-sonnet-20241022\n"
        "tools:\n"
        "- read\n"
        "- write\n"
        "---\n\n"
        "# Helper\n\nHelp the user.\n",
        encoding="utf-8",
    )

    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 1
    target = tmp_path / ".kiro" / "agents" / "helper.md"
    content = target.read_text(encoding="utf-8")
    assert "description: My helpful agent" in content
    assert "model: claude-3-5-sonnet-20241022" in content
    assert "- read" in content
    assert "- write" in content
    assert "# Helper" in content
    assert "Help the user." in content


def test_kiro_agents_strip_name_and_unknown_frontmatter(tmp_path: Path) -> None:
    """name and unknown fields are stripped; only description/model/tools kept."""
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    (apm_agents / "specialist.agent.md").write_text(
        "---\n"
        "name: specialist\n"
        "description: A specialist agent\n"
        "color: blue\n"
        "someunknown: ignored\n"
        "tools:\n"
        "- shell\n"
        "---\n\n"
        "Specialist body.\n",
        encoding="utf-8",
    )

    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 1
    target = tmp_path / ".kiro" / "agents" / "specialist.md"
    content = target.read_text(encoding="utf-8")
    assert "name:" not in content
    assert "color:" not in content
    assert "someunknown:" not in content
    assert "description: A specialist agent" in content
    assert "- shell" in content
    assert "Specialist body." in content


def test_kiro_agents_nested_path_identity_derivation(tmp_path: Path) -> None:
    """Nested path in .apm/agents/ is preserved under .kiro/agents/."""
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    nested = package_dir / ".apm" / "agents" / "team"
    nested.mkdir(parents=True)
    (nested / "architect.agent.md").write_text(
        "---\ndescription: Team architect\n---\n\n# Architect\n",
        encoding="utf-8",
    )

    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 1
    target = tmp_path / ".kiro" / "agents" / "team" / "architect.md"
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "description: Team architect" in content
    assert "# Architect" in content


def test_kiro_agents_fail_closed_incompatible_tools(tmp_path: Path) -> None:
    """Agent with incompatible tools is not deployed and emits an error."""
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    (apm_agents / "badtools.agent.md").write_text(
        "---\n"
        "description: Agent with bad tools\n"
        "tools:\n"
        "- read\n"
        "- execute_arbitrary_code\n"
        "---\n\n"
        "# Bad Tools Agent\n",
        encoding="utf-8",
    )

    diagnostics = DiagnosticCollector()
    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
        diagnostics=diagnostics,
    )

    # Agent must not be deployed (fail closed)
    assert result.files_integrated == 0
    assert not (tmp_path / ".kiro" / "agents" / "badtools.md").exists()
    # Error diagnostic must name the unsupported tool
    errors = diagnostics.by_category().get("error", [])
    assert errors, "Expected an error diagnostic for incompatible tools"
    assert "execute_arbitrary_code" in errors[0].message


def test_kiro_agents_no_partial_write_on_incompatible_tools(tmp_path: Path) -> None:
    """Fail closed: no partial agent file written when tools are invalid."""
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    (apm_agents / "partial.agent.md").write_text(
        "---\ntools:\n- read\n- INVALID_TOOL\n---\n\nBody.\n",
        encoding="utf-8",
    )

    AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert not (tmp_path / ".kiro" / "agents" / "partial.md").exists()


def test_kiro_agents_all_approved_tools_pass(tmp_path: Path) -> None:
    """Every value in KIRO_AGENT_ALLOWED_TOOLS is accepted without error."""
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    tools_list = sorted(KIRO_AGENT_ALLOWED_TOOLS - {"*"})
    tools_yaml = "\n".join(f"- {t}" for t in tools_list)
    (apm_agents / "alltools.agent.md").write_text(
        f"---\ndescription: All tools\ntools:\n{tools_yaml}\n---\n\nBody.\n",
        encoding="utf-8",
    )

    diagnostics = DiagnosticCollector()
    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
        diagnostics=diagnostics,
    )

    assert result.files_integrated == 1
    errors = diagnostics.by_category().get("error", [])
    assert not errors


def test_kiro_agents_wildcard_tool_passes(tmp_path: Path) -> None:
    """The '*' wildcard tool is accepted."""
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    (apm_agents / "fullaccess.agent.md").write_text(
        "---\ntools:\n- '*'\n---\n\nBody.\n",
        encoding="utf-8",
    )

    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 1
    assert (tmp_path / ".kiro" / "agents" / "fullaccess.md").exists()


def test_kiro_agents_skip_when_no_kiro_dir(tmp_path: Path) -> None:
    """Agent integration is skipped when .kiro/ does not exist."""
    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    (apm_agents / "agent.agent.md").write_text("# Agent\n", encoding="utf-8")

    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 0
    assert not (tmp_path / ".kiro").exists()


def test_kiro_agents_idempotent_second_deploy(tmp_path: Path) -> None:
    """Second deploy with the same source produces identical output."""
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    (apm_agents / "worker.agent.md").write_text(
        "---\ndescription: Worker\ntools:\n- read\n---\n\n# Worker\n",
        encoding="utf-8",
    )

    integrator = AgentIntegrator()
    pi = _make_package_info(package_dir)

    result1 = integrator.integrate_agents_for_target(KNOWN_TARGETS["kiro"], pi, tmp_path)
    assert result1.files_integrated == 1
    first_content = (tmp_path / ".kiro" / "agents" / "worker.md").read_text(encoding="utf-8")

    # Second deploy (simulate managed_files from lockfile)
    managed = {".kiro/agents/worker.md"}
    integrator.integrate_agents_for_target(
        KNOWN_TARGETS["kiro"], pi, tmp_path, managed_files=managed
    )
    second_content = (tmp_path / ".kiro" / "agents" / "worker.md").read_text(encoding="utf-8")

    assert first_content == second_content


def test_kiro_agents_update_replaces_stale_file(tmp_path: Path) -> None:
    """Updated source replaces the stale deployed agent."""
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    agent_src = apm_agents / "evolving.agent.md"
    agent_src.write_text("---\ndescription: v1\n---\n\n# v1\n", encoding="utf-8")

    integrator = AgentIntegrator()
    pi = _make_package_info(package_dir)
    target = tmp_path / ".kiro" / "agents" / "evolving.md"

    integrator.integrate_agents_for_target(KNOWN_TARGETS["kiro"], pi, tmp_path)
    assert "v1" in target.read_text(encoding="utf-8")

    agent_src.write_text("---\ndescription: v2\n---\n\n# v2\n", encoding="utf-8")
    managed = {".kiro/agents/evolving.md"}
    integrator.integrate_agents_for_target(
        KNOWN_TARGETS["kiro"], pi, tmp_path, managed_files=managed
    )
    assert "v2" in target.read_text(encoding="utf-8")
    assert "v1" not in target.read_text(encoding="utf-8")


def test_kiro_agents_coexist_with_hooks_and_steering(tmp_path: Path) -> None:
    """Kiro agents coexist with steering and hooks under .kiro/."""
    kiro_dir = tmp_path / ".kiro"
    kiro_dir.mkdir()
    (kiro_dir / "steering").mkdir()
    (kiro_dir / "steering" / "global.md").write_text(
        "---\ninclusion: always\n---\n\nGlobal rules.\n",
        encoding="utf-8",
    )

    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    (apm_agents / "helper.agent.md").write_text(
        "---\ndescription: Helper\n---\n\n# Helper\n",
        encoding="utf-8",
    )

    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 1
    assert (tmp_path / ".kiro" / "agents" / "helper.md").exists()
    # Existing steering file is untouched
    assert (tmp_path / ".kiro" / "steering" / "global.md").exists()


def test_kiro_agents_sync_removes_managed_file(tmp_path: Path) -> None:
    """sync_for_target removes APM-managed Kiro agent files."""
    from apm_cli.models.apm_package import APMPackage

    (tmp_path / ".kiro" / "agents").mkdir(parents=True)
    managed_agent = tmp_path / ".kiro" / "agents" / "obsolete.md"
    managed_agent.write_text("---\ndescription: Old\n---\n\n# Old\n", encoding="utf-8")

    pkg = APMPackage(
        name="test-pkg",
        version="1.0.0",
        package_path=tmp_path,
        source="github.com/test/test-pkg",
    )
    managed_files = {".kiro/agents/obsolete.md"}

    result = AgentIntegrator().sync_for_target(
        KNOWN_TARGETS["kiro"],
        pkg,
        tmp_path,
        managed_files=managed_files,
    )

    assert result["files_removed"] == 1
    assert not managed_agent.exists()


def test_kiro_agents_preflight_before_adopt_bypass(tmp_path: Path) -> None:
    """reg-tg-009 regression: invalid-tools agent is not adopted via byte-match.

    Pre-place a .kiro/agents/X.md whose bytes match the source (which has
    an unsupported tool). The adopt fast-path MUST NOT fire; the agent must
    remain in files_skipped and not appear in target_paths.
    """
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    bad_src_content = "---\ntools:\n- read\n- BANNED_TOOL\n---\n\n# Agent\n"
    (apm_agents / "agent.agent.md").write_text(bad_src_content, encoding="utf-8")

    # Pre-seed target with the SAME bytes as source (the bypass scenario).
    kiro_agents = tmp_path / ".kiro" / "agents"
    kiro_agents.mkdir(parents=True)
    preset_target = kiro_agents / "agent.md"
    preset_target.write_text(bad_src_content, encoding="utf-8")

    diagnostics = DiagnosticCollector()
    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
        managed_files={".kiro/agents/agent.md"},
        diagnostics=diagnostics,
    )

    assert result.files_integrated == 0
    assert result.files_adopted == 0
    assert preset_target not in result.target_paths
    errors = diagnostics.by_category().get(CATEGORY_ERROR, [])
    assert errors, "Expected diagnostics.error for incompatible tools even in adopt path"
    assert "BANNED_TOOL" in errors[0].message


def test_kiro_agents_no_agents_dir_created_for_all_invalid(tmp_path: Path) -> None:
    """No .kiro/agents/ directory is created when the only agent has invalid tools."""
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    (apm_agents / "bad.agent.md").write_text(
        "---\ntools:\n- INVALID\n---\n\nBad agent.\n",
        encoding="utf-8",
    )

    AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    # The .kiro/agents/ directory must NOT be created.
    assert not (tmp_path / ".kiro" / "agents").exists()


def test_kiro_agents_tools_whitespace_stripped_in_output(tmp_path: Path) -> None:
    """Whitespace around tool values must be stripped in deployed frontmatter.

    Regression for the normalization gap: validation stripped leading/trailing
    whitespace before checking the allowlist, but the original (unstripped)
    values were written to disk.  After the fix, the deployed file must contain
    the stripped values, not the source values with surrounding whitespace.
    """
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    # Source agent with whitespace-padded tool values.
    (apm_agents / "ws.agent.md").write_text(
        "---\ndescription: WS test\ntools:\n- ' read '\n- ' write '\n---\n\nBody.\n",
        encoding="utf-8",
    )

    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 1, "Agent with whitespace-padded tools should deploy"
    deployed = (tmp_path / ".kiro" / "agents" / "ws.md").read_text(encoding="utf-8")
    # Stripped values must be in the deployed file, not the original padded ones.
    assert "read" in deployed
    assert "write" in deployed
    # The raw padded values must not appear verbatim in the output.
    assert " read " not in deployed
    assert " write " not in deployed


def test_kiro_agents_stale_managed_file_removed_when_tools_become_incompatible(
    tmp_path: Path,
) -> None:
    """Stale APM-managed Kiro agent is removed when its tools become incompatible.

    Regression: if a source agent was previously deployed with valid tools and
    its source is later updated to declare incompatible tools, the fail-closed
    gate blocks the new deployment.  The stale deployed file must be removed
    through the normal sync reconcile when the agent is no longer in the new
    target_paths list and the caller passes the managed set.

    This test validates the unit contract: integrate_agents_for_target with
    incompatible tools skips the agent and does NOT add it to target_paths,
    leaving the reconcile caller free to remove the stale path.
    """
    from apm_cli.models.apm_package import APMPackage

    (tmp_path / ".kiro" / "agents").mkdir(parents=True)
    stale_path = tmp_path / ".kiro" / "agents" / "prev.md"
    stale_path.write_text("---\ndescription: Old valid\n---\n\n# Old\n", encoding="utf-8")

    package_dir = tmp_path / "pkg"
    apm_agents = package_dir / ".apm" / "agents"
    apm_agents.mkdir(parents=True)
    # Source now has incompatible tools.
    (apm_agents / "prev.agent.md").write_text(
        "---\ndescription: Now bad\ntools:\n- EVIL_TOOL\n---\n\n# Bad\n",
        encoding="utf-8",
    )

    diagnostics = DiagnosticCollector()
    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
        managed_files={".kiro/agents/prev.md"},
        diagnostics=diagnostics,
    )

    # The new integration must skip the agent (incompatible tools).
    assert result.files_integrated == 0
    assert result.files_skipped == 1
    # The stale path must NOT appear in target_paths; the caller can remove it.
    assert stale_path not in result.target_paths
    errors = diagnostics.by_category().get(CATEGORY_ERROR, [])
    assert errors, "Incompatible tools must produce an error diagnostic"
    assert "EVIL_TOOL" in errors[0].message

    # Reconcile: simulate what the install service does -- remove stale managed files
    # that no longer appear in target_paths.
    pkg = APMPackage(
        name="test-pkg",
        version="1.0.0",
        package_path=tmp_path,
        source="github.com/test/test-pkg",
    )
    sync_result = AgentIntegrator().sync_for_target(
        KNOWN_TARGETS["kiro"],
        pkg,
        tmp_path,
        managed_files={".kiro/agents/prev.md"},
    )
    assert sync_result["files_removed"] == 1, (
        "sync_for_target must remove the stale APM-managed file"
    )
    assert not stale_path.exists(), "Stale managed Kiro agent must be removed after sync"


# ---------------------------------------------------------------------------
# Kiro CLI prompts (#3216) - .kiro/prompts/<name>.md
# ---------------------------------------------------------------------------


def test_kiro_prompts_deploy_to_kiro_prompts_dir(tmp_path: Path) -> None:
    """Prompts are deployed to .kiro/prompts/ when the kiro target is used."""
    (tmp_path / ".kiro").mkdir()
    package_dir = tmp_path / "pkg"
    apm_prompts = package_dir / ".apm" / "prompts"
    apm_prompts.mkdir(parents=True)
    (apm_prompts / "review.prompt.md").write_text(
        "---\ndescription: Review code\n---\n\n# Review\n\nReview the code.\n",
        encoding="utf-8",
    )

    result = PromptIntegrator().integrate_prompts_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 1
    target = tmp_path / ".kiro" / "prompts" / "review.md"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "\n# Review\n\nReview the code.\n"


def test_kiro_prompts_skip_when_no_kiro_dir(tmp_path: Path) -> None:
    """Prompt integration is skipped when .kiro/ does not exist."""
    package_dir = tmp_path / "pkg"
    apm_prompts = package_dir / ".apm" / "prompts"
    apm_prompts.mkdir(parents=True)
    (apm_prompts / "review.prompt.md").write_text(
        "---\ndescription: Review\n---\n\n# Review\n",
        encoding="utf-8",
    )

    result = PromptIntegrator().integrate_prompts_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 0
    assert not (tmp_path / ".kiro").exists()


def test_kiro_prompts_primitive_mapping(tmp_path: Path) -> None:
    """Verify the kiro target profile has a prompts primitive mapping."""
    target = KNOWN_TARGETS["kiro"]
    assert "prompts" in target.primitives
    mapping = target.primitives["prompts"]
    assert mapping.subdir == "prompts"
    assert mapping.extension == ".md"
    assert mapping.format_id == "kiro_prompt"
    assert mapping.output_compare is False


def test_kiro_prompts_sync_removes_managed_file(tmp_path: Path) -> None:
    """sync_for_target removes APM-managed Kiro prompt files."""
    (tmp_path / ".kiro" / "prompts").mkdir(parents=True)
    managed_prompt = tmp_path / ".kiro" / "prompts" / "obsolete.md"
    managed_prompt.write_text(
        "---\ndescription: Old\n---\n\n# Old\n",
        encoding="utf-8",
    )

    pkg = APMPackage(
        name="test-pkg",
        version="1.0.0",
        package_path=tmp_path,
        source="github.com/test/test-pkg",
    )
    managed_files = {".kiro/prompts/obsolete.md"}

    result = PromptIntegrator().sync_for_target(
        KNOWN_TARGETS["kiro"],
        pkg,
        tmp_path,
        managed_files=managed_files,
    )

    assert result["files_removed"] == 1
    assert not managed_prompt.exists()


def test_kiro_prompts_coexist_with_agents_and_hooks(tmp_path: Path) -> None:
    """Kiro prompts coexist with agents and hooks under .kiro/."""
    kiro_dir = tmp_path / ".kiro"
    kiro_dir.mkdir()
    (kiro_dir / "agents").mkdir()
    (kiro_dir / "hooks").mkdir()

    package_dir = tmp_path / "pkg"
    apm_prompts = package_dir / ".apm" / "prompts"
    apm_prompts.mkdir(parents=True)
    (apm_prompts / "review.prompt.md").write_text(
        "---\ndescription: Review\n---\n\n# Review\n",
        encoding="utf-8",
    )

    result = PromptIntegrator().integrate_prompts_for_target(
        KNOWN_TARGETS["kiro"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 1
    assert (tmp_path / ".kiro" / "prompts" / "review.md").exists()
    # Existing agents and hooks directories are untouched
    assert (tmp_path / ".kiro" / "agents").exists()
    assert (tmp_path / ".kiro" / "hooks").exists()


@pytest.fixture
def kiro_prompt_source(tmp_path: Path) -> Path:
    """Author one real source prompt outside the selected native target."""
    (tmp_path / ".kiro").mkdir()
    source = tmp_path / "pkg" / ".apm" / "prompts" / "review.prompt.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nname: ignored\ndescription: Review\n---\nReview ${1}\n")
    return source


def _install_prompt(tmp_path: Path, **kwargs):
    return PromptIntegrator().integrate_prompts_for_target(
        KNOWN_TARGETS["kiro"], _make_package_info(tmp_path / "pkg"), tmp_path, **kwargs
    )


def test_kiro_prompt_body_arguments_and_loss_diagnostics(
    tmp_path: Path, kiro_prompt_source: Path
) -> None:
    body = "\nReview ${1} ${10} $ARGUMENTS ${@} ${input:name}\n\n"
    kiro_prompt_source.write_bytes(
        ("\ufeff---\nname: ignored\nmodel: ignored\ntools: [read]\n---\n" + body)
        .replace("\n", "\r\n")
        .encode("utf-8")
    )
    diagnostics = DiagnosticCollector()
    result = _install_prompt(tmp_path, diagnostics=diagnostics)
    assert result.files_integrated == 1
    assert (tmp_path / ".kiro/prompts/review.md").read_bytes() == body.encode()
    assert not (tmp_path / ".kiro/prompts/ignored.md").exists()
    warnings = diagnostics.by_category()[CATEGORY_WARNING]
    assert len(warnings) == 2
    assert "model, tools" in warnings[0].message
    assert "${input:name}" in warnings[1].message


@pytest.mark.windows_compat
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("force", [False, True])
def test_kiro_prompt_collision_adoption_and_force(
    tmp_path: Path, kiro_prompt_source: Path, force: bool, newline: str
) -> None:
    target = tmp_path / ".kiro/prompts/review.md"
    target.parent.mkdir()
    target.write_text("User-authored prompt\n")
    result = _install_prompt(tmp_path, force=force)
    assert result.files_integrated == int(force)
    assert result.files_skipped == int(not force)
    assert target.read_text() == ("Review ${1}\n" if force else "User-authored prompt\n")
    original_bytes = f"Review ${{1}}{newline}".encode()
    target.write_bytes(original_bytes)
    before = target.stat().st_mtime_ns
    adopted = _install_prompt(tmp_path)
    assert adopted.files_adopted == int(newline == "\n")
    assert adopted.files_skipped == int(newline != "\n")
    assert adopted.target_paths == ([target] if newline == "\n" else [])
    assert target.read_bytes() == original_bytes
    assert target.stat().st_mtime_ns == before


@pytest.mark.parametrize(
    "content", ["---\nmodel: [\n---\nBad\n", "---\ninterval: daily\n---\nWorkflow\n"]
)
def test_kiro_prompt_rejects_invalid_or_workflow_source_before_adoption(
    tmp_path: Path, kiro_prompt_source: Path, content: str
) -> None:
    kiro_prompt_source.write_text(content)
    target = tmp_path / ".kiro/prompts/review.md"
    target.parent.mkdir()
    target.write_text(content)
    result = _install_prompt(tmp_path, force=True)
    assert result.files_skipped == 1
    assert result.files_adopted == 0
    assert result.target_paths == []
    assert target.read_text() == content


@pytest.mark.parametrize("component", ["root", "prompts", "file", "internal-file", "source"])
def test_kiro_prompt_rejects_symlink_writes_even_with_force(
    tmp_path: Path, kiro_prompt_source: Path, component: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "review.md"
    sentinel.write_text("untouched\n")
    target = tmp_path / ".kiro/prompts/review.md"
    if component == "root":
        (tmp_path / ".kiro").rmdir()
        (tmp_path / ".kiro").symlink_to(outside, target_is_directory=True)
    elif component == "prompts":
        target.parent.symlink_to(outside, target_is_directory=True)
    elif component in {"file", "internal-file"}:
        target.parent.mkdir()
        if component == "internal-file":
            sentinel = target.parent / "user.md"
            sentinel.write_text("untouched\n")
        target.symlink_to(sentinel)
    else:
        kiro_prompt_source.unlink()
        kiro_prompt_source.symlink_to(sentinel)
    result = _install_prompt(tmp_path, force=True)
    assert result.files_integrated == 0
    assert result.target_paths == []
    assert sentinel.read_text() == "untouched\n"
    assert not (outside / "prompts").exists()


def test_kiro_prompt_resolves_body_links_and_preserves_copilot_source(
    tmp_path: Path, kiro_prompt_source: Path
) -> None:
    from urllib.parse import unquote, urlparse

    content = "---\ndescription: Review\n---\n[Guide](../../guide.md)\n"
    kiro_prompt_source.write_text(content)
    (tmp_path / "pkg/guide.md").write_text("Guide\n")
    result = _install_prompt(tmp_path)
    assert result.links_resolved == 1
    deployed = (tmp_path / ".kiro/prompts/review.md").read_text()
    link = deployed.split("](", 1)[1].split(")", 1)[0]
    parsed = urlparse(link)
    assert parsed.scheme == ""
    assert parsed.netloc == ""
    assert (tmp_path / ".kiro/prompts" / unquote(parsed.path)).resolve() == (
        tmp_path / "pkg/guide.md"
    ).resolve()
    PromptIntegrator().integrate_prompts_for_target(
        KNOWN_TARGETS["copilot"], _make_package_info(tmp_path / "pkg"), tmp_path
    )
    copilot = (tmp_path / ".github/prompts/review.prompt.md").read_text()
    assert copilot.startswith("---\ndescription: Review\n---\n")
    assert kiro_prompt_source.read_text() == content
