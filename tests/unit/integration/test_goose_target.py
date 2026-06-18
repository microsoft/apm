"""Path-fidelity acceptance tests for the experimental Goose (Block) target.

These tests lock the resolved deploy surface for Goose against its official
config docs (https://goose-docs.ai). Goose is unlike every other target on
two structural points, both asserted here:

  - MCP servers live in a single YAML home config
    (~/.config/goose/config.yaml, honouring $XDG_CONFIG_HOME) under an
    ``extensions:`` key, with a Goose-native per-server schema
    (type: stdio / cmd / args / envs / enabled / timeout) -- NOT the JSON
    ``mcpServers`` schema. Written by GooseClientAdapter at user scope.
  - Instructions are a single ``.goosehints`` stub at the project root that
    imports the AGENTS.md roll-up via Goose's ``@./AGENTS.md`` preprocessor
    (compile_family="agents"), NOT a per-file rules directory.

Activation is EXPERIMENTAL (flag "goose"): never part of ``--target all``.
"""

from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path

import pytest

from apm_cli.adapters.client.goose import GooseClientAdapter
from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig
from apm_cli.compilation.goose_formatter import GooseFormatter
from apm_cli.core.target_detection import (
    ALL_CANONICAL_TARGETS,
    EXPERIMENTAL_TARGETS,
    should_compile_agents_md,
    should_compile_goose_hints,
)
from apm_cli.factory import ClientFactory
from apm_cli.integration.agent_integrator import AgentIntegrator
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
    package_dir: Path, name: str = "test-pkg", package_type: PackageType | None = None
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
# Target profile shape
# ---------------------------------------------------------------------------


def test_goose_profile_matches_official_surface() -> None:
    target = KNOWN_TARGETS["goose"]

    assert target.name == "goose"
    assert target.root_dir == ".goose"  # display-grouping placeholder only
    assert target.compile_family == "agents"  # emits AGENTS.md (imported by .goosehints)
    assert target.requires_flag == "goose"  # experimental
    assert target.user_supported == "partial"  # skills at user scope; recipes project-only
    assert target.auto_create is True  # explicit --target goose creates .goose/recipes/
    assert target.detect_by_dir is False

    # agents -> Goose recipes (.goose/recipes/*.yaml); skills -> .agents/skills/.
    # MCP is handled by the adapter; instructions by the compile stub.
    assert set(target.primitives) == {"agents", "skills"}

    agents = target.primitives["agents"]
    assert agents.subdir == "recipes"
    assert agents.extension == ".yaml"
    assert agents.format_id == "goose_recipe"

    skills = target.primitives["skills"]
    assert skills.format_id == "skill_standard"
    assert skills.deploy_root == ".agents"  # cross-tool standard Goose reads natively

    # Recipes have no canonical user-scope home -> project-scope only.
    assert target.unsupported_user_primitives == ("agents",)


# ---------------------------------------------------------------------------
# Experimental activation: flag-gated, never in "all"
# ---------------------------------------------------------------------------


def test_goose_is_experimental_not_in_all() -> None:
    assert "goose" in EXPERIMENTAL_TARGETS
    assert "goose" not in ALL_CANONICAL_TARGETS


def test_goose_excluded_from_target_all(tmp_path: Path) -> None:
    names = {p.name for p in active_targets(tmp_path, "all")}

    assert "goose" not in names
    # Sanity: canonical single-tool targets are still present in "all".
    assert {"claude", "gemini", "kiro"} <= names


def test_goose_resolves_only_when_named_with_flag_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apm_cli.core.experimental.is_enabled", lambda name: name == "goose")

    profiles = active_targets(tmp_path, "goose")

    assert [p.name for p in profiles] == ["goose"]


# ---------------------------------------------------------------------------
# Factory wiring
# ---------------------------------------------------------------------------


def test_goose_factory_returns_goose_adapter() -> None:
    adapter = ClientFactory.create_client("goose", user_scope=True)

    assert isinstance(adapter, GooseClientAdapter)
    assert "goose" in ClientFactory.supported_clients()
    assert adapter.target_name == "goose"
    assert adapter.mcp_servers_key == "extensions"
    assert adapter.supports_user_scope is True


# ---------------------------------------------------------------------------
# MCP adapter: config path honours XDG_CONFIG_HOME
# ---------------------------------------------------------------------------


def test_goose_config_path_honours_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    adapter = GooseClientAdapter(user_scope=True)
    assert adapter.get_config_path() == str(tmp_path / "xdg" / "goose" / "config.yaml")


def test_goose_config_path_defaults_to_dot_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    adapter = GooseClientAdapter(user_scope=True)
    assert adapter.get_config_path() == str(tmp_path / ".config" / "goose" / "config.yaml")


# ---------------------------------------------------------------------------
# MCP adapter: per-server schema transform (Copilot-format -> Goose extensions)
# ---------------------------------------------------------------------------


def test_goose_stdio_transform() -> None:
    out = GooseClientAdapter()._to_native_format(
        "github",
        {
            "type": "local",
            "command": "npx",
            "args": ["-y", "srv"],
            "env": {"K": "v"},
            "tools": ["*"],
        },
    )
    assert out == {
        "name": "github",
        "type": "stdio",
        "cmd": "npx",
        "args": ["-y", "srv"],
        "envs": {"K": "v"},
        "enabled": True,
        "timeout": 300,
    }


def test_goose_remote_transform_maps_to_streamable_http() -> None:
    out = GooseClientAdapter()._to_native_format(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer x"},
        },
    )
    assert out["type"] == "streamable_http"
    assert out["uri"] == "https://example.com/mcp"
    assert out["headers"] == {"Authorization": "Bearer x"}
    assert "cmd" not in out and "args" not in out


# ---------------------------------------------------------------------------
# MCP adapter: YAML write -- 0o600, sibling preservation, malformed refusal
# ---------------------------------------------------------------------------


def _adapter_with_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> GooseClientAdapter:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return GooseClientAdapter(user_scope=True)


def test_goose_update_config_writes_extensions_block_0600(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = _adapter_with_home(monkeypatch, tmp_path)

    assert adapter.update_config({"srv": {"command": "uvx", "args": ["pkg"], "env": {}}}) is True

    path = Path(adapter.get_config_path())
    from apm_cli.utils.yaml_io import load_yaml

    data = load_yaml(path)
    assert data["extensions"]["srv"]["type"] == "stdio"
    assert data["extensions"]["srv"]["cmd"] == "uvx"
    # Credential-bearing config must be owner-only.
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_goose_update_config_preserves_unrelated_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = _adapter_with_home(monkeypatch, tmp_path)
    path = Path(adapter.get_config_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "GOOSE_PROVIDER: openai\n"
        "extensions:\n"
        "  preexisting:\n"
        "    name: preexisting\n"
        "    type: stdio\n"
        "    cmd: echo\n"
        "    enabled: true\n",
        encoding="utf-8",
    )

    adapter.update_config({"github": {"command": "npx", "args": ["-y", "srv"], "env": {}}})

    from apm_cli.utils.yaml_io import load_yaml

    data = load_yaml(path)
    assert data["GOOSE_PROVIDER"] == "openai"  # native key preserved
    assert set(data["extensions"]) == {"preexisting", "github"}


def test_goose_update_config_refuses_malformed_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = _adapter_with_home(monkeypatch, tmp_path)
    path = Path(adapter.get_config_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("just a string, not a mapping\n", encoding="utf-8")

    assert adapter.update_config({"github": {"command": "npx", "args": []}}) is False
    # File left untouched.
    assert path.read_text(encoding="utf-8") == "just a string, not a mapping\n"


# ---------------------------------------------------------------------------
# Compile: .goosehints stub imports AGENTS.md
# ---------------------------------------------------------------------------


def test_goose_hints_compiles_only_for_explicit_goose() -> None:
    assert should_compile_goose_hints("goose") is True
    assert should_compile_goose_hints("all") is False
    assert should_compile_goose_hints(frozenset({"agents"})) is False
    # AGENTS.md (the imported roll-up) is still emitted for goose.
    assert should_compile_agents_md("goose") is True


def test_goose_formatter_emits_dot_goosehints_stub() -> None:
    formatter = GooseFormatter(".")
    assert formatter._stub_filename == ".goosehints"
    content = formatter._generate_stub()
    assert "@./AGENTS.md" in content


def test_goose_compile_writes_agents_md_and_goosehints(tmp_path: Path) -> None:
    instructions = tmp_path / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "core.instructions.md").write_text(
        '---\napplyTo: "**"\n---\n# Core\nBe concise.\n', encoding="utf-8"
    )

    result = AgentsCompiler(base_dir=str(tmp_path)).compile(CompilationConfig(target="goose"))

    assert result.success
    assert (tmp_path / "AGENTS.md").exists()
    hints = tmp_path / ".goosehints"
    assert hints.exists()
    assert "@./AGENTS.md" in hints.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Agents -> Goose recipes (.goose/recipes/<name>.yaml)
# ---------------------------------------------------------------------------


def test_goose_agent_compiles_to_recipe_yaml(tmp_path: Path) -> None:
    package_dir = tmp_path / "pkg"
    agents_dir = package_dir / ".apm" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "security-review.agent.md").write_text(
        "---\n"
        "name: security-review\n"
        "description: Reviews diffs for OWASP issues.\n"
        "model: gpt-5\n"
        "---\n\n"
        "You are a security reviewer. Inspect the diff.\n",
        encoding="utf-8",
    )

    result = AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["goose"], _make_package_info(package_dir), tmp_path
    )

    assert result.files_integrated == 1
    recipe_path = tmp_path / ".goose" / "recipes" / "security-review.yaml"
    assert recipe_path.exists()

    import yaml as _yaml

    recipe = _yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    assert recipe["version"] == "1.0.0"
    assert recipe["title"] == "security-review"
    assert recipe["description"] == "Reviews diffs for OWASP issues."
    assert recipe["instructions"] == "You are a security reviewer. Inspect the diff."
    # A pinned model becomes settings.goose_model.
    assert recipe["settings"] == {"goose_model": "gpt-5"}
    # MCP extensions are NOT embedded (agents declare no MCP servers).
    assert "extensions" not in recipe


def test_goose_recipe_omits_settings_without_model(tmp_path: Path) -> None:
    package_dir = tmp_path / "pkg"
    agents_dir = package_dir / ".apm" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "helper.agent.md").write_text(
        "---\nname: helper\ndescription: A helper.\n---\n\nDo helpful things.\n",
        encoding="utf-8",
    )

    AgentIntegrator().integrate_agents_for_target(
        KNOWN_TARGETS["goose"], _make_package_info(package_dir), tmp_path
    )

    import yaml as _yaml

    recipe = _yaml.safe_load(
        (tmp_path / ".goose" / "recipes" / "helper.yaml").read_text(encoding="utf-8")
    )
    assert "settings" not in recipe
    assert recipe["instructions"] == "Do helpful things."


# ---------------------------------------------------------------------------
# Skills -> .agents/skills/<pkg>/SKILL.md (cross-tool standard)
# ---------------------------------------------------------------------------


def test_goose_skills_deploy_to_agents_skills(tmp_path: Path) -> None:
    package_dir = tmp_path / "skill-pkg"
    package_dir.mkdir()
    (package_dir / "SKILL.md").write_text(
        "---\nname: skill-pkg\ndescription: Demo skill\n---\n\n# Demo\n",
        encoding="utf-8",
    )

    result = SkillIntegrator().integrate_package_skill(
        _make_package_info(package_dir, "skill-pkg", PackageType.CLAUDE_SKILL),
        tmp_path,
        targets=[KNOWN_TARGETS["goose"]],
    )

    assert result.skill_created is True
    target = tmp_path / ".agents" / "skills" / "skill-pkg" / "SKILL.md"
    assert target.exists()


# ---------------------------------------------------------------------------
# Scope: skills at user scope -> ~/.agents/skills/; recipes are project-only
# ---------------------------------------------------------------------------


def test_goose_user_scope_keeps_skills_drops_recipes() -> None:
    user_profile = KNOWN_TARGETS["goose"].for_scope(user_scope=True)

    assert user_profile is not None
    assert "skills" in user_profile.primitives  # ~/.agents/skills/ via deploy_root
    assert "agents" not in user_profile.primitives  # recipes are project-scope only
