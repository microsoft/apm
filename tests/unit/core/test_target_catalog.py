"""Characterization tests for target capability metadata."""

from contextlib import nullcontext
from dataclasses import FrozenInstanceError, astuple
from types import MappingProxyType

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.core.apm_yml import CANONICAL_TARGETS
from apm_cli.core.target_catalog import (
    TARGET_CAPABILITIES,
    TargetCapability,
    _build_target_catalog,
    accepted_target_values,
    expand_all,
    get_target_capability,
    normalize_target_name,
    target_error_values,
    target_help_fragment,
)
from apm_cli.core.target_detection import (
    ALL_CANONICAL_TARGETS,
    EXPERIMENTAL_TARGETS,
    EXPLICIT_ONLY_TARGETS,
    MCP_ONLY_TARGETS,
    TARGET_ALIASES,
    VALID_TARGET_VALUES,
    parse_target_field,
)
from apm_cli.integration.targets import KNOWN_TARGETS, RUNTIME_TO_CANONICAL_TARGET

COMMANDS = ("compile", "install", "update")


def test_current_target_sets_and_aliases_are_characterized() -> None:
    """Lock the accepted target contract before moving its owner."""
    assert (
        frozenset({"claude", "codex", "cursor", "gemini", "kiro", "opencode", "vscode", "windsurf"})
        == ALL_CANONICAL_TARGETS
    )
    assert (
        frozenset({"copilot-app", "copilot-cowork", "hermes", "openclaw"}) == EXPERIMENTAL_TARGETS
    )
    assert frozenset({"agent-skills", "antigravity"}) == EXPLICIT_ONLY_TARGETS
    assert frozenset({"intellij"}) == MCP_ONLY_TARGETS
    assert TARGET_ALIASES == {
        "agy": "antigravity",
        "agents": "vscode",
        "copilot": "vscode",
        "vscode": "vscode",
    }
    assert (
        frozenset(
            {
                "agent-skills",
                "agents",
                "agy",
                "all",
                "antigravity",
                "claude",
                "codex",
                "copilot",
                "copilot-app",
                "copilot-cowork",
                "cursor",
                "gemini",
                "hermes",
                "intellij",
                "kiro",
                "openclaw",
                "opencode",
                "vscode",
                "windsurf",
            }
        )
        == VALID_TARGET_VALUES
    )


def test_current_runtime_mapping_is_characterized() -> None:
    """Lock runtime-to-native-profile routing before moving its owner."""
    assert RUNTIME_TO_CANONICAL_TARGET == {
        "agents": "copilot",
        "intellij": "copilot",
        "vscode": "copilot",
    }


def test_current_native_profiles_are_characterized() -> None:
    """Lock native roots, primitive mappings, flags, and compile families."""
    actual = {
        name: (
            profile.root_dir,
            {primitive: astuple(mapping) for primitive, mapping in profile.primitives.items()},
            profile.compile_family,
            profile.requires_flag,
        )
        for name, profile in KNOWN_TARGETS.items()
    }
    assert actual == {
        "copilot": (
            ".github",
            {
                "instructions": (
                    "instructions",
                    ".instructions.md",
                    "github_instructions",
                    None,
                    False,
                ),
                "prompts": ("prompts", ".prompt.md", "github_prompt", None, False),
                "agents": ("agents", ".agent.md", "github_agent", None, False),
                "skills": ("skills", "/SKILL.md", "skill_standard", ".agents", False),
                "hooks": ("hooks", ".json", "github_hooks", None, False),
                "canvas": ("extensions", "", "copilot_canvas", None, False),
            },
            "vscode",
            None,
        ),
        "claude": (
            ".claude",
            {
                "instructions": ("rules", ".md", "claude_rules", None, True),
                "agents": ("agents", ".md", "claude_agent", None, False),
                "commands": ("commands", ".md", "claude_command", None, False),
                "skills": ("skills", "/SKILL.md", "skill_standard", None, False),
                "hooks": ("hooks", ".json", "claude_hooks", None, False),
            },
            "claude",
            None,
        ),
        "cursor": (
            ".cursor",
            {
                "instructions": ("rules", ".mdc", "cursor_rules", None, True),
                "agents": ("agents", ".md", "cursor_agent", None, False),
                "commands": ("commands", ".md", "claude_command", None, False),
                "skills": ("skills", "/SKILL.md", "skill_standard", ".agents", False),
                "hooks": ("hooks", ".json", "cursor_hooks", None, False),
            },
            "agents",
            None,
        ),
        "kiro": (
            ".kiro",
            {
                "instructions": ("steering", ".md", "kiro_steering", None, True),
                "skills": ("skills", "/SKILL.md", "skill_standard", None, False),
                "hooks": ("hooks", ".json", "kiro_hooks", None, False),
            },
            "agents",
            None,
        ),
        "opencode": (
            ".opencode",
            {
                "agents": ("agents", ".md", "opencode_agent", None, False),
                "commands": ("commands", ".md", "opencode_command", None, False),
                "skills": ("skills", "/SKILL.md", "skill_standard", ".agents", False),
            },
            "agents",
            None,
        ),
        "gemini": (
            ".gemini",
            {
                "commands": ("commands", ".toml", "gemini_command", None, False),
                "skills": ("skills", "/SKILL.md", "skill_standard", ".agents", False),
                "hooks": ("hooks", ".json", "gemini_hooks", None, False),
            },
            "gemini",
            None,
        ),
        "antigravity": (
            ".agents",
            {
                "instructions": ("rules", ".md", "antigravity_rules", None, True),
                "skills": ("skills", "/SKILL.md", "skill_standard", None, False),
                "hooks": ("", "hooks.json", "antigravity_hooks", None, False),
            },
            "agents",
            None,
        ),
        "codex": (
            ".codex",
            {
                "agents": ("agents", ".toml", "codex_agent", None, False),
                "skills": ("skills", "/SKILL.md", "skill_standard", ".agents", False),
                "hooks": ("", "hooks.json", "codex_hooks", None, False),
            },
            "agents",
            None,
        ),
        "windsurf": (
            ".windsurf",
            {
                "instructions": ("rules", ".md", "windsurf_rules", None, True),
                "skills": ("skills", "/SKILL.md", "skill_standard", ".agents", False),
                "commands": ("workflows", ".md", "windsurf_workflow", None, False),
                "hooks": ("", "hooks.json", "windsurf_hooks", None, False),
            },
            "agents",
            None,
        ),
        "agent-skills": (
            ".agents",
            {"skills": ("skills", "/SKILL.md", "skill_standard", None, False)},
            None,
            None,
        ),
        "openclaw": (
            ".agents",
            {"skills": ("skills", "/SKILL.md", "skill_standard", None, False)},
            None,
            "openclaw",
        ),
        "hermes": (
            ".agents",
            {"skills": ("skills", "/SKILL.md", "skill_standard", None, False)},
            "agents",
            "hermes",
        ),
        "copilot-cowork": (
            "copilot-cowork",
            {"skills": ("skills", "/SKILL.md", "skill_standard", None, False)},
            None,
            "copilot_cowork",
        ),
        "copilot-app": (
            "copilot-app",
            {"prompts": ("workflows", ".prompt.md", "prompt_standard", None, False)},
            None,
            "copilot_app",
        ),
    }


def test_catalog_is_immutable_and_projects_accepted_values() -> None:
    """The catalog owns every accepted target value."""
    assert isinstance(TARGET_CAPABILITIES, MappingProxyType)
    assert accepted_target_values() == VALID_TARGET_VALUES
    assert accepted_target_values("install") == VALID_TARGET_VALUES
    assert accepted_target_values("update") == VALID_TARGET_VALUES
    assert accepted_target_values("compile") == frozenset(
        {
            "agents",
            "agy",
            "all",
            "antigravity",
            "claude",
            "codex",
            "copilot",
            "gemini",
            "kiro",
            "opencode",
            "vscode",
            "windsurf",
        }
    )


def test_every_accepted_value_is_advertised_and_parses() -> None:
    """Help and error advertising cannot drift from parser acceptance."""
    for command in COMMANDS:
        help_fragment = target_help_fragment(command)
        error_values = target_error_values(command)
        assert frozenset(error_values) == accepted_target_values(command)
        for value in accepted_target_values(command):
            assert value in help_fragment
            with pytest.warns(Warning) if value == "agents" else nullcontext():
                assert parse_target_field(value) is not None


@pytest.mark.parametrize("command", COMMANDS)
def test_command_help_and_errors_advertise_every_accepted_value(command: str) -> None:
    """Real Click surfaces advertise the same values accepted by parsing."""
    runner = CliRunner()
    help_result = runner.invoke(cli, [command, "--help"], terminal_width=1000)
    error_result = runner.invoke(
        cli,
        [command, "--target", "definitely-bogus"],
        terminal_width=1000,
    )

    assert help_result.exit_code == 0
    assert error_result.exit_code == 2
    valid_line = next(
        line for line in error_result.output.splitlines() if line.startswith("Valid targets:")
    )
    error_values = {target.strip() for target in valid_line.partition(":")[2].split(",")}
    assert error_values == set(accepted_target_values(command))
    for value in accepted_target_values(command):
        assert value in help_result.output


def test_alias_runtime_profile_and_compile_projections_match_catalog() -> None:
    """Catalog metadata reproduces every legacy projection."""
    assert normalize_target_name("vscode") == "copilot"
    assert normalize_target_name("agents") == "copilot"
    assert normalize_target_name("agy") == "antigravity"
    assert get_target_capability("vscode") is TARGET_CAPABILITIES["copilot"]

    catalog_runtime_map = {
        runtime: capability.primitive_profile
        for capability in TARGET_CAPABILITIES.values()
        for runtime in capability.runtimes
    }
    assert catalog_runtime_map == RUNTIME_TO_CANONICAL_TARGET
    for name, profile in KNOWN_TARGETS.items():
        capability = TARGET_CAPABILITIES[name]
        assert profile.capability is capability
        assert profile.name == capability.name
        assert capability.primitive_profile == name
        assert capability.compile_family == profile.compile_family
        assert capability.experimental_flag == profile.requires_flag
        with pytest.raises(FrozenInstanceError):
            profile.name = "changed"


def test_all_excludes_explicit_experimental_and_mcp_only_targets() -> None:
    """The all expansion preserves the stable implicit target set."""
    assert frozenset(expand_all("install")) == ALL_CANONICAL_TARGETS
    assert frozenset(expand_all("update")) == ALL_CANONICAL_TARGETS
    assert frozenset(expand_all("compile")) == frozenset(
        {"claude", "codex", "gemini", "kiro", "opencode", "vscode", "windsurf"}
    )
    for command in COMMANDS:
        expanded = frozenset(expand_all(command))
        assert not expanded & EXPERIMENTAL_TARGETS
        assert not expanded & EXPLICIT_ONLY_TARGETS
        assert not expanded & MCP_ONLY_TARGETS


def test_apm_yml_canonical_targets_project_catalog_profiles() -> None:
    """Manifest validation accepts stable native deployment profiles."""
    assert (
        frozenset(
            capability.name
            for capability in TARGET_CAPABILITIES.values()
            if capability.experimental_flag is None and not capability.mcp_only
        )
        == CANONICAL_TARGETS
    )


def test_catalog_validation_rejects_duplicate_aliases() -> None:
    """No accepted value may resolve to multiple capabilities."""
    first = _capability("one", aliases=("shared",))
    second = _capability("two", aliases=("shared",))
    with pytest.raises(ValueError, match=r"Duplicate target value 'shared'"):
        _build_target_catalog((first, second))


def test_catalog_validation_rejects_missing_mcp_primitive_profile() -> None:
    """MCP-only capabilities must name their native primitive profile."""
    invalid = _capability("mcp-only", mcp_only=True, primitive_profile=None)
    with pytest.raises(ValueError, match=r"MCP-only target 'mcp-only'.*primitive_profile"):
        _build_target_catalog((invalid,))


def test_catalog_validation_rejects_duplicate_runtime_and_invalid_all_membership() -> None:
    """Runtimes are unique and gated targets cannot participate in all."""
    with pytest.raises(ValueError, match=r"Runtime 'same'.*multiple targets"):
        _build_target_catalog(
            (
                _capability("one", runtimes=("same",)),
                _capability("two", runtimes=("same",)),
            )
        )
    with pytest.raises(ValueError, match=r"Target 'gated'.*in_all"):
        _build_target_catalog((_capability("gated", in_all=True, experimental_flag="flag"),))


def _capability(
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    in_all: bool = False,
    experimental_flag: str | None = None,
    mcp_only: bool = False,
    primitive_profile: str | None = "profile",
    runtimes: tuple[str, ...] = (),
) -> TargetCapability:
    """Build compact validation fixtures with otherwise valid metadata."""
    return TargetCapability(
        name=name,
        aliases=aliases,
        description=f"{name} target",
        in_all=in_all,
        explicit_only=False,
        experimental_flag=experimental_flag,
        mcp_only=mcp_only,
        primitive_profile=primitive_profile,
        compile_family=None,
        runtimes=runtimes,
        commands=frozenset(COMMANDS),
    )
