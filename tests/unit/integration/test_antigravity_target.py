"""Path-fidelity acceptance tests for the Antigravity (agy) target (#1650).

These tests lock the resolved deploy path / file / native shape for every
Antigravity primitive against the official agy config surface
(https://antigravity.google/docs). They are the regression trap that the
original PR lacked: a wrong path or a fabricated primitive must fail here.

Authoritative surface (antigravity.google/docs):
  - instructions -> AGENTS.md (compile) + .agents/rules/ (installed rules)
  - skills       -> .agents/skills/ (workspace) / ~/.gemini/antigravity-cli/skills/ (user)
  - MCP          -> .agents/mcp_config.json (workspace) / ~/.gemini/config/mcp_config.json (user)
  - hooks        -> .agents/hooks.json in Antigravity's OWN native schema
  - commands     -> NONE (legacy Gemini commands convert to skills upstream)
Activation is EXPLICIT-ONLY: .agents/ is the shared cross-tool root, so there is
no Antigravity-unique signal -> never auto-detected, never part of `--target all`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from apm_cli.adapters.client.antigravity import AntigravityClientAdapter
from apm_cli.core.target_detection import (
    ALL_CANONICAL_TARGETS,
    EXPLICIT_ONLY_TARGETS,
    normalize_target_list,
)
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS, active_targets
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


# ---------------------------------------------------------------------------
# Target profile: shape, root, and the explicit-only activation model
# ---------------------------------------------------------------------------


def test_antigravity_profile_matches_official_surface() -> None:
    target = KNOWN_TARGETS["antigravity"]

    assert target.root_dir == ".agents"
    assert target.compile_family == "agents"  # emits AGENTS.md, not GEMINI.md
    assert target.auto_create is True
    assert target.detect_by_dir is False  # shared root -> no auto-detect signal
    assert target.user_supported == "partial"
    assert target.user_root_dir == ".gemini/antigravity-cli"
    assert target.hooks_config_display == ".agents/hooks.json"

    # Commands MUST NOT exist -- agy has no TOML command surface.
    assert set(target.primitives) == {"instructions", "skills", "hooks"}
    assert "commands" not in target.primitives

    instructions = target.primitives["instructions"]
    assert instructions.subdir == "rules"  # .agents/rules/
    assert instructions.extension == ".md"
    assert instructions.format_id == "antigravity_rules"

    skills = target.primitives["skills"]
    assert skills.subdir == "skills"  # .agents/skills/ (root_dir is .agents)
    assert skills.extension == "/SKILL.md"
    assert skills.format_id == "skill_standard"
    assert skills.deploy_root is None  # inherits the .agents root directly

    hooks = target.primitives["hooks"]
    assert hooks.subdir == ""  # file sits at the .agents root
    assert hooks.extension == "hooks.json"
    assert hooks.format_id == "antigravity_hooks"


def test_antigravity_is_explicit_only_not_in_all() -> None:
    assert "antigravity" in EXPLICIT_ONLY_TARGETS
    assert "antigravity" not in ALL_CANONICAL_TARGETS


def test_antigravity_excluded_from_target_all(tmp_path: Path) -> None:
    names = {p.name for p in active_targets(tmp_path, "all")}

    assert "antigravity" not in names
    # Sanity: canonical single-tool targets are still present in "all".
    assert {"claude", "gemini", "kiro"} <= names


def test_antigravity_resolves_only_when_named_explicitly(tmp_path: Path) -> None:
    profiles = active_targets(tmp_path, "antigravity")

    assert [p.name for p in profiles] == ["antigravity"]


def test_agy_alias_normalizes_to_antigravity() -> None:
    assert normalize_target_list("agy") == ["antigravity"]
    assert normalize_target_list("antigravity") == ["antigravity"]


def test_antigravity_never_auto_detected_from_shared_agents_dir(tmp_path: Path) -> None:
    # The shared .agents/ root exists, but antigravity must NOT be auto-detected
    # off it -- only agent-skills-style explicit selection activates the target.
    (tmp_path / ".agents").mkdir()

    detected = {p.name for p in active_targets(tmp_path, None)}

    assert "antigravity" not in detected


# ---------------------------------------------------------------------------
# Instructions -> .agents/rules/<name>.md (plain markdown, frontmatter stripped)
# ---------------------------------------------------------------------------


def test_antigravity_instructions_deploy_to_agents_rules(tmp_path: Path) -> None:
    (tmp_path / ".agents").mkdir()
    package_dir = tmp_path / "pkg"
    instructions_dir = package_dir / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    (instructions_dir / "style.instructions.md").write_text(
        "---\n"
        "description: Style rules\n"
        'applyTo: "src/**/*.py"\n'
        "---\n\n"
        "# Style\n\nUse type hints.\n",
        encoding="utf-8",
    )

    result = InstructionIntegrator().integrate_instructions_for_target(
        KNOWN_TARGETS["antigravity"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 1
    target = tmp_path / ".agents" / "rules" / "style.md"
    assert target.exists()
    # Antigravity rules now carry trigger: glob frontmatter.
    assert target.read_text(encoding="utf-8") == (
        '---\ntrigger: glob\nglobs: "src/**/*.py"\n---\n\n# Style\n\nUse type hints.\n'
    )


def test_antigravity_instructions_deploy_multi_glob_to_agents_rules(tmp_path: Path) -> None:
    (tmp_path / ".agents").mkdir()
    package_dir = tmp_path / "pkg"
    instructions_dir = package_dir / ".apm" / "instructions"
    instructions_dir.mkdir(parents=True)
    (instructions_dir / "style.instructions.md").write_text(
        "---\n"
        "description: Style rules\n"
        'applyTo: "src/**/*.py, tests/**/*.py"\n'
        "---\n\n"
        "# Style\n\nUse type hints.\n",
        encoding="utf-8",
    )

    result = InstructionIntegrator().integrate_instructions_for_target(
        KNOWN_TARGETS["antigravity"],
        _make_package_info(package_dir),
        tmp_path,
    )

    assert result.files_integrated == 1
    target = tmp_path / ".agents" / "rules" / "style.md"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == (
        "---\n"
        "trigger: glob\n"
        "globs:\n"
        '  - "src/**/*.py"\n'
        '  - "tests/**/*.py"\n'
        "---\n\n"
        "# Style\n\nUse type hints.\n"
    )


# -----------------------------------------------------------------------------------------------------------
# Skills -> .agents/skills/<pkg>/SKILL.md
# ---------------------------------------------------------------------------


def test_antigravity_skills_deploy_to_agents_skills(tmp_path: Path) -> None:
    (tmp_path / ".agents").mkdir()
    package_dir = tmp_path / "skill-pkg"
    package_dir.mkdir()
    (package_dir / "SKILL.md").write_text(
        "---\nname: skill-pkg\ndescription: Demo skill\n---\n\n# Demo\n",
        encoding="utf-8",
    )

    result = SkillIntegrator().integrate_package_skill(
        _make_package_info(package_dir, "skill-pkg", PackageType.CLAUDE_SKILL),
        tmp_path,
        targets=[KNOWN_TARGETS["antigravity"]],
    )

    target = tmp_path / ".agents" / "skills" / "skill-pkg" / "SKILL.md"
    assert result.skill_created is True
    assert target.read_text(encoding="utf-8") == (
        "---\nname: skill-pkg\ndescription: Demo skill\n---\n\n# Demo\n"
    )


# ---------------------------------------------------------------------------
# MCP -> dedicated mcp_config.json (NOT settings.json)
# ---------------------------------------------------------------------------


def test_antigravity_mcp_project_path_is_agents_mcp_config(tmp_path: Path) -> None:
    adapter = AntigravityClientAdapter(project_root=tmp_path, user_scope=False)

    assert adapter.target_name == "antigravity"
    assert Path(adapter.get_config_path()) == tmp_path / ".agents" / "mcp_config.json"


def test_antigravity_mcp_user_path_is_gemini_config_mcp_config() -> None:
    adapter = AntigravityClientAdapter(user_scope=True)

    assert Path(adapter.get_config_path()) == (
        Path.home() / ".gemini" / "config" / "mcp_config.json"
    )


def test_antigravity_mcp_writes_mcp_servers_to_dedicated_file(tmp_path: Path) -> None:
    (tmp_path / ".agents").mkdir()
    adapter = AntigravityClientAdapter(project_root=tmp_path, user_scope=False)

    adapter.update_config({"demo": {"command": "npx", "args": ["-y", "demo"]}})

    config_file = tmp_path / ".agents" / "mcp_config.json"
    settings_file = tmp_path / ".agents" / "settings.json"
    assert config_file.exists()
    # settings.json must NEVER carry mcpServers for Antigravity.
    assert not settings_file.exists()
    data = json.loads(config_file.read_text(encoding="utf-8"))
    assert data["mcpServers"]["demo"]["command"] == "npx"


# ---------------------------------------------------------------------------
# Hooks -> .agents/hooks.json in Antigravity's OWN native schema
# ---------------------------------------------------------------------------


def _seed_antigravity_hook_package(tmp_path: Path, name: str) -> Path:
    package_dir = tmp_path / name
    hooks_dir = package_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    hook_data = {
        "hooks": {
            # Nested event: matcher + hooks[] handler list.
            "PreToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python ${PLUGIN_ROOT}/hooks/check.py",
                            "timeoutSec": 15,
                        }
                    ]
                }
            ],
            # Flat event: bare handler list (matcher ignored).
            "Stop": [
                {
                    "type": "command",
                    "command": "python ${PLUGIN_ROOT}/hooks/done.py",
                    "timeoutSec": 20,
                }
            ],
        }
    }
    (hooks_dir / "hooks.json").write_text(json.dumps(hook_data), encoding="utf-8")
    (hooks_dir / "check.py").write_text("# check\n", encoding="utf-8")
    (hooks_dir / "done.py").write_text("# done\n", encoding="utf-8")
    return package_dir


def test_antigravity_hooks_merge_into_native_hooks_json(tmp_path: Path) -> None:
    (tmp_path / ".agents").mkdir()
    package_dir = _seed_antigravity_hook_package(tmp_path, "agyhooks")

    result = HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["antigravity"],
        _make_package_info(package_dir, "agyhooks"),
        tmp_path,
    )

    assert result.files_integrated >= 1

    hooks_file = tmp_path / ".agents" / "hooks.json"
    assert hooks_file.exists()
    # Single native file -- never settings.json.
    assert not (tmp_path / ".agents" / "settings.json").exists()

    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    # APM owns the reserved "apm" hook-name container; events nest beneath it.
    assert "apm" in data
    container = data["apm"]

    # PreToolUse keeps the nested {matcher?, hooks:[handler...]} shape.
    pre = container["PreToolUse"]
    assert isinstance(pre, list) and len(pre) == 1
    assert "hooks" in pre[0]
    pre_handler = pre[0]["hooks"][0]
    assert pre_handler["command"] == "python .agents/hooks/agyhooks/hooks/check.py"
    # timeout stays in SECONDS (no ms conversion, unlike Gemini).
    assert pre_handler["timeout"] == 15

    # Stop is a FLAT handler list -- no matcher / hooks[] wrapper.
    stop = container["Stop"]
    assert isinstance(stop, list) and len(stop) == 1
    stop_handler = stop[0]
    assert "hooks" not in stop_handler
    assert stop_handler["command"] == "python .agents/hooks/agyhooks/hooks/done.py"
    assert stop_handler["timeout"] == 20

    # Scripts land under the .agents hooks tree.
    assert (tmp_path / ".agents" / "hooks" / "agyhooks" / "hooks" / "check.py").exists()
    assert (tmp_path / ".agents" / "hooks" / "agyhooks" / "hooks" / "done.py").exists()


def test_antigravity_hooks_preserve_sibling_user_hook_names(tmp_path: Path) -> None:
    (tmp_path / ".agents").mkdir()
    # A user-authored hook under its own top-level name must survive the merge.
    existing = {
        "my-user-hook": {
            "Stop": [{"type": "command", "command": "echo bye"}],
        }
    }
    hooks_file = tmp_path / ".agents" / "hooks.json"
    hooks_file.write_text(json.dumps(existing), encoding="utf-8")

    package_dir = _seed_antigravity_hook_package(tmp_path, "agyhooks")
    HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["antigravity"],
        _make_package_info(package_dir, "agyhooks"),
        tmp_path,
    )

    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    # Both the user's hook-name and APM's reserved container coexist.
    assert data["my-user-hook"]["Stop"][0]["command"] == "echo bye"
    assert "apm" in data


def test_antigravity_hooks_skip_when_agents_dir_absent(tmp_path: Path) -> None:
    # require_dir=True: no .agents/ -> no write, no fabricated file.
    package_dir = _seed_antigravity_hook_package(tmp_path, "agyhooks")

    result = HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["antigravity"],
        _make_package_info(package_dir, "agyhooks"),
        tmp_path,
    )

    assert result.files_integrated == 0
    assert not (tmp_path / ".agents" / "hooks.json").exists()


def test_antigravity_instructions_included_without_rules_dir(tmp_path: Path) -> None:
    """Without .agents/rules/, instructions appear in AGENTS.md."""
    from apm_cli.utils.yaml_io import dump_yaml

    # Minimal apm.yml so discovery works
    dump_yaml({"name": "test-project", "version": "1.0.0"}, tmp_path / "apm.yml")
    # Create .apm/instructions with a sample instruction
    instr_dir = tmp_path / ".apm" / "instructions"
    instr_dir.mkdir(parents=True)
    (instr_dir / "style.instructions.md").write_text(
        "---\ndescription: Style guide\napplyTo: '**/*.py'\n---\nUse type hints.\n",
        encoding="utf-8",
    )

    from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig

    compiler = AgentsCompiler(str(tmp_path))
    config = CompilationConfig(target="antigravity", dry_run=False)
    result = compiler.compile(config)
    assert result.success
    agents_md = tmp_path / "AGENTS.md"
    assert agents_md.exists()
    assert "Use type hints." in agents_md.read_text(encoding="utf-8")


def test_antigravity_instructions_skipped_with_populated_rules_dir(tmp_path: Path) -> None:
    """With .agents/rules/ containing .md files, instructions are skipped from AGENTS.md."""
    from apm_cli.utils.yaml_io import dump_yaml

    # Minimal apm.yml so discovery works
    dump_yaml({"name": "test-project", "version": "1.0.0"}, tmp_path / "apm.yml")
    # Create .apm/instructions with a sample instruction
    instr_dir = tmp_path / ".apm" / "instructions"
    instr_dir.mkdir(parents=True)
    (instr_dir / "style.instructions.md").write_text(
        "---\ndescription: Style guide\napplyTo: '**/*.py'\n---\nUse type hints.\n",
        encoding="utf-8",
    )

    rules_dir = tmp_path / ".agents" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "style.md").write_text(
        "---\ntrigger: glob\nglobs: '**/*.py'\n---\nUse type hints.\n",
        encoding="utf-8",
    )

    from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig

    compiler = AgentsCompiler(str(tmp_path))
    config = CompilationConfig(target="antigravity", dry_run=False)
    result = compiler.compile(config)
    assert result.success

    # No constitution or other content, so AGENTS.md should not be generated
    agents_md = tmp_path / "AGENTS.md"
    assert not agents_md.exists()
