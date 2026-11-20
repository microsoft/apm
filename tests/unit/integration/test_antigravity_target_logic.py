"""Path-fidelity acceptance tests for the Antigravity (agy) target (#1650) (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.core.target_detection import (
    ALL_CANONICAL_TARGETS,
    EXPLICIT_ONLY_TARGETS,
    normalize_target_list,
)
from apm_cli.integration.targets import KNOWN_TARGETS

pytestmark = pytest.mark.unit


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
    assert target.user_root_dir == ".gemini/config"
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


def test_agy_alias_normalizes_to_antigravity() -> None:
    assert normalize_target_list("agy") == ["antigravity"]
    assert normalize_target_list("antigravity") == ["antigravity"]


# ---------------------------------------------------------------------------
# Instructions -> .agents/rules/<name>.md (trigger/globs frontmatter)
# ---------------------------------------------------------------------------


# -----------------------------------------------------------------------------------------------------------
# Skills -> .agents/skills/<pkg>/SKILL.md
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCP -> dedicated mcp_config.json (NOT settings.json)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Hooks -> .agents/hooks.json in Antigravity's OWN native schema
# ---------------------------------------------------------------------------
