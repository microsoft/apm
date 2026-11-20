"""Tests for active_targets() resolution in targets.py (pure logic cases)."""

from pathlib import Path

import pytest

from apm_cli.integration.targets import (
    KNOWN_TARGETS,
    RULE_FORMATS,
    PrimitiveMapping,
)

pytestmark = pytest.mark.unit


class TestPrimitiveMappingValidation:
    """output_compare must stay in lockstep with RULE_FORMATS (apm#1662)."""

    @pytest.mark.parametrize("fmt", sorted(RULE_FORMATS))
    def test_rule_format_without_output_compare_raises(self, fmt):
        with pytest.raises(ValueError, match="output_compare=True"):
            PrimitiveMapping("rules", ".md", fmt)

    @pytest.mark.parametrize("fmt", sorted(RULE_FORMATS))
    def test_rule_format_with_output_compare_ok(self, fmt):
        m = PrimitiveMapping("rules", ".md", fmt, output_compare=True)
        assert m.output_compare is True

    def test_output_compare_on_non_rule_format_raises(self):
        with pytest.raises(ValueError, match="not a known rule format"):
            PrimitiveMapping("instructions", ".md", "copilot", output_compare=True)

    def test_non_rule_format_default_ok(self):
        m = PrimitiveMapping("agents", ".md", "claude_agent")
        assert m.output_compare is False

    def test_known_rule_targets_set_output_compare(self):
        for name in ("claude", "cursor", "windsurf"):
            mapping = KNOWN_TARGETS[name].primitives["instructions"]
            assert mapping.format_id in RULE_FORMATS
            assert mapping.output_compare is True


# ---------------------------------------------------------------------------
# Skill routing convergence (apm#1520)
# ---------------------------------------------------------------------------


class TestDefaultSkillRouting:
    """Assert that the documented clients route skills to .agents/ by default."""

    def test_default_skill_routing_uses_agents_dir_for_documented_clients(self):
        """Shared skill targets use .agents while grok-cloud stays native."""
        expected = {
            "copilot": ".agents",
            "cursor": ".agents",
            "opencode": ".agents",
            "codex": ".agents",
            "gemini": ".agents",
            "windsurf": ".agents",
            "grok-build": None,
            "grok-cloud": None,
            "claude": None,  # not documented as .agents/-aware
        }
        for name, want_root in expected.items():
            profile = KNOWN_TARGETS[name]
            skills_pm = profile.primitives.get("skills")
            assert skills_pm is not None, f"{name} should have skills primitive"
            assert skills_pm.deploy_root == want_root, (
                f"{name}: expected deploy_root={want_root!r}, got {skills_pm.deploy_root!r}"
            )

    def test_legacy_skill_paths_flag_restores_per_client_routing(self):
        """With apply_legacy_skill_paths(), deploy_root is reset to None."""
        from apm_cli.integration.targets import apply_legacy_skill_paths

        profiles = [
            KNOWN_TARGETS[n]
            for n in ("copilot", "cursor", "opencode", "codex", "claude", "gemini", "windsurf")
        ]
        restored = apply_legacy_skill_paths(profiles)

        # All 7 should have deploy_root=None after legacy restore
        for profile in restored:
            skills_pm = profile.primitives.get("skills")
            assert skills_pm is not None, f"{profile.name} should have skills"
            assert skills_pm.deploy_root is None, (
                f"{profile.name}: expected deploy_root=None (legacy), got {skills_pm.deploy_root!r}"
            )

    def test_claude_skills_unchanged_by_default(self):
        """Explicit guard: claude keeps its native skill routing."""
        profile = KNOWN_TARGETS["claude"]
        skills_pm = profile.primitives["skills"]
        assert skills_pm.deploy_root is None, (
            f"claude: deploy_root should be None (native routing), got {skills_pm.deploy_root!r}"
        )

    def test_gemini_skill_routing_uses_agents_dir_by_default(self):
        """Gemini CLI docs list .agents/skills/ as the preferred alias."""
        profile = KNOWN_TARGETS["gemini"]
        skills_pm = profile.primitives["skills"]
        assert skills_pm.deploy_root == ".agents", (
            f"gemini: expected deploy_root='.agents', got {skills_pm.deploy_root!r}"
        )

    def test_gemini_legacy_skill_paths_restores_per_client_routing(self):
        """With apply_legacy_skill_paths(), gemini deploy_root is reset to None."""
        from apm_cli.integration.targets import apply_legacy_skill_paths

        profiles = [KNOWN_TARGETS["gemini"]]
        restored = apply_legacy_skill_paths(profiles)
        skills_pm = restored[0].primitives["skills"]
        assert skills_pm.deploy_root is None, (
            f"gemini: expected deploy_root=None (legacy), got {skills_pm.deploy_root!r}"
        )

    def test_windsurf_skill_routing_uses_agents_dir_by_default(self):
        """Windsurf (now Devin Desktop) converges skills onto .agents/skills/ (apm#1520)."""
        profile = KNOWN_TARGETS["windsurf"]
        skills_pm = profile.primitives["skills"]
        assert skills_pm.deploy_root == ".agents", (
            f"windsurf: expected deploy_root='.agents', got {skills_pm.deploy_root!r}"
        )
        # The deploy directory is <effective_root>/skills/<name>; with the
        # override that resolves to .agents/skills/ (parts, not substring).
        effective_root = skills_pm.deploy_root or profile.root_dir
        resolved = Path(effective_root) / "skills"
        assert resolved.parts == (".agents", "skills"), (
            f"windsurf: expected .agents/skills/, got {resolved.parts!r}"
        )

    def test_windsurf_legacy_skill_paths_restores_per_client_routing(self):
        """With apply_legacy_skill_paths(), windsurf skills return to .windsurf/skills/ (apm#1520)."""
        from apm_cli.integration.targets import apply_legacy_skill_paths

        profiles = [KNOWN_TARGETS["windsurf"]]
        restored = apply_legacy_skill_paths(profiles)
        profile = restored[0]
        skills_pm = profile.primitives["skills"]
        assert skills_pm.deploy_root is None, (
            f"windsurf: expected deploy_root=None (legacy), got {skills_pm.deploy_root!r}"
        )
        effective_root = skills_pm.deploy_root or profile.root_dir
        resolved = Path(effective_root) / "skills"
        assert resolved.parts == (".windsurf", "skills"), (
            f"windsurf legacy: expected .windsurf/skills/, got {resolved.parts!r}"
        )

    def test_apply_legacy_does_not_mutate_known_targets(self):
        """apply_legacy_skill_paths must not mutate the global KNOWN_TARGETS."""
        from apm_cli.integration.targets import apply_legacy_skill_paths

        original_root = KNOWN_TARGETS["copilot"].primitives["skills"].deploy_root
        profiles = [KNOWN_TARGETS["copilot"]]
        apply_legacy_skill_paths(profiles)
        assert KNOWN_TARGETS["copilot"].primitives["skills"].deploy_root == original_root
