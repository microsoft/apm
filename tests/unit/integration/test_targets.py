"""Tests for active_targets() resolution in targets.py."""

import shutil
import tempfile
from pathlib import Path

from apm_cli.integration.targets import KNOWN_TARGETS, active_targets


class TestActiveTargets:
    """Verify active_targets() presence-based detection and fallback."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -- auto-detect (no explicit target) --

    def test_nothing_exists_falls_back_to_copilot(self):
        targets = active_targets(self.root)
        assert len(targets) == 1
        assert targets[0].name == "copilot"

    def test_only_github_returns_copilot(self):
        (self.root / ".github").mkdir()
        targets = active_targets(self.root)
        assert [t.name for t in targets] == ["copilot"]

    def test_only_claude_returns_claude(self):
        (self.root / ".claude").mkdir()
        targets = active_targets(self.root)
        assert [t.name for t in targets] == ["claude"]

    def test_only_cursor_returns_cursor(self):
        (self.root / ".cursor").mkdir()
        targets = active_targets(self.root)
        assert [t.name for t in targets] == ["cursor"]

    def test_only_opencode_returns_opencode(self):
        (self.root / ".opencode").mkdir()
        targets = active_targets(self.root)
        assert [t.name for t in targets] == ["opencode"]

    def test_github_and_claude_returns_both(self):
        (self.root / ".github").mkdir()
        (self.root / ".claude").mkdir()
        targets = active_targets(self.root)
        names = {t.name for t in targets}
        assert names == {"copilot", "claude"}

    def test_all_four_dirs_returns_all_four(self):
        for d in (".github", ".claude", ".cursor", ".opencode"):
            (self.root / d).mkdir()
        targets = active_targets(self.root)
        assert len(targets) == 4

    def test_claude_and_cursor_without_github(self):
        (self.root / ".claude").mkdir()
        (self.root / ".cursor").mkdir()
        targets = active_targets(self.root)
        names = {t.name for t in targets}
        assert "copilot" not in names
        assert names == {"claude", "cursor"}

    # -- explicit target --

    def test_explicit_copilot(self):
        targets = active_targets(self.root, explicit_target="copilot")
        assert [t.name for t in targets] == ["copilot"]

    def test_explicit_claude(self):
        targets = active_targets(self.root, explicit_target="claude")
        assert [t.name for t in targets] == ["claude"]

    def test_explicit_all_returns_every_known_target(self):
        from apm_cli.core.target_detection import (
            EXPERIMENTAL_TARGETS,
            EXPLICIT_ONLY_TARGETS,
        )

        targets = active_targets(self.root, explicit_target="all")
        expected = len(KNOWN_TARGETS) - len(EXPLICIT_ONLY_TARGETS) - len(EXPERIMENTAL_TARGETS)
        assert len(targets) == expected
        names = [t.name for t in targets]
        assert "copilot-cowork" not in names
        assert "agent-skills" not in names

    def test_explicit_vscode_alias(self):
        targets = active_targets(self.root, explicit_target="vscode")
        assert [t.name for t in targets] == ["copilot"]

    def test_explicit_agents_alias(self):
        targets = active_targets(self.root, explicit_target="agents")
        assert [t.name for t in targets] == ["copilot"]

    def test_explicit_overrides_detection(self):
        """Explicit target wins even if dirs for other targets exist."""
        (self.root / ".github").mkdir()
        (self.root / ".claude").mkdir()
        targets = active_targets(self.root, explicit_target="claude")
        assert [t.name for t in targets] == ["claude"]

    def test_unknown_target_raises_at_parse_time(self):
        """Unknown tokens in apm.yml or --target must fail at the parser.

        Replaces the previous ``test_explicit_unknown_returns_empty`` --
        the silent-empty contract was the root cause of #820 (apm install
        and apm compile exited 0 while deploying nothing).
        """
        import pytest

        from apm_cli.core.target_detection import parse_target_field

        with pytest.raises(ValueError, match="not a valid target"):
            parse_target_field("nonexistent")

    # -- codex detection --

    def test_only_codex_returns_codex(self):
        (self.root / ".codex").mkdir()
        targets = active_targets(self.root)
        assert [t.name for t in targets] == ["codex"]

    def test_explicit_codex(self):
        targets = active_targets(self.root, explicit_target="codex")
        assert [t.name for t in targets] == ["codex"]

    def test_codex_not_detected_when_only_agents_dir_exists(self):
        """Only .agents/ existing (no .codex/) should NOT detect Codex."""
        (self.root / ".agents").mkdir()
        targets = active_targets(self.root)
        # .agents/ alone doesn't match any target root_dir
        assert len(targets) == 1
        assert targets[0].name == "copilot"  # fallback

    # -- gemini detection --

    def test_only_gemini_returns_gemini(self):
        (self.root / ".gemini").mkdir()
        targets = active_targets(self.root)
        assert [t.name for t in targets] == ["gemini"]

    def test_explicit_gemini(self):
        targets = active_targets(self.root, explicit_target="gemini")
        assert [t.name for t in targets] == ["gemini"]

    def test_gemini_and_claude_returns_both(self):
        (self.root / ".gemini").mkdir()
        (self.root / ".claude").mkdir()
        targets = active_targets(self.root)
        names = {t.name for t in targets}
        assert names == {"gemini", "claude"}

    def test_all_seven_dirs_returns_all_seven(self):
        for d in (".github", ".claude", ".cursor", ".opencode", ".codex", ".gemini", ".windsurf"):
            (self.root / d).mkdir()
        targets = active_targets(self.root)
        assert len(targets) == 7

    def test_all_five_dirs_returns_all_five(self):
        for d in (".github", ".claude", ".cursor", ".opencode", ".codex"):
            (self.root / d).mkdir()
        targets = active_targets(self.root)
        assert len(targets) == 5

    # -- windsurf detection --

    def test_only_windsurf_returns_windsurf(self):
        (self.root / ".windsurf").mkdir()
        targets = active_targets(self.root)
        assert [t.name for t in targets] == ["windsurf"]

    def test_explicit_windsurf(self):
        targets = active_targets(self.root, explicit_target="windsurf")
        assert [t.name for t in targets] == ["windsurf"]

    def test_windsurf_and_github_returns_both(self):
        (self.root / ".windsurf").mkdir()
        (self.root / ".github").mkdir()
        targets = active_targets(self.root)
        names = {t.name for t in targets}
        assert names == {"windsurf", "copilot"}

    # -- explicit list of targets --

    def test_explicit_list_single_target(self):
        targets = active_targets(self.root, explicit_target=["claude"])
        assert [t.name for t in targets] == ["claude"]

    def test_explicit_list_multiple_targets(self):
        targets = active_targets(self.root, explicit_target=["claude", "copilot"])
        assert [t.name for t in targets] == ["claude", "copilot"]

    def test_explicit_list_deduplicates_aliases(self):
        """copilot and vscode are aliases -- should return one profile."""
        targets = active_targets(self.root, explicit_target=["copilot", "vscode"])
        assert [t.name for t in targets] == ["copilot"]

    def test_explicit_list_with_all_returns_every_known_target(self):
        from apm_cli.core.target_detection import (
            EXPERIMENTAL_TARGETS,
            EXPLICIT_ONLY_TARGETS,
        )

        targets = active_targets(self.root, explicit_target=["all"])
        expected = len(KNOWN_TARGETS) - len(EXPLICIT_ONLY_TARGETS) - len(EXPERIMENTAL_TARGETS)
        assert len(targets) == expected

    def test_explicit_list_all_mixed_returns_every_known_target(self):
        """'all' anywhere in the list wins."""
        from apm_cli.core.target_detection import (
            EXPERIMENTAL_TARGETS,
            EXPLICIT_ONLY_TARGETS,
        )

        targets = active_targets(self.root, explicit_target=["claude", "all"])
        expected = len(KNOWN_TARGETS) - len(EXPLICIT_ONLY_TARGETS) - len(EXPERIMENTAL_TARGETS)
        assert len(targets) == expected

    def test_explicit_list_all_unknown_returns_empty(self):
        """When the parser is bypassed and all tokens are unknown, the
        result is an empty list -- the old asymmetric ``[copilot]`` fallback
        was removed in #820 because the parser
        (:func:`apm_cli.core.target_detection.parse_target_field`) now
        rejects unknown tokens at the entry point."""
        targets = active_targets(self.root, explicit_target=["nonexistent", "bogus"])
        assert targets == []

    def test_explicit_list_mixed_known_unknown(self):
        """Known targets are included, unknown ones are skipped (no fallback).

        In normal use the parser rejects this input upstream; this test
        exercises the post-parser invariant that the loop only adds known
        profiles.
        """
        targets = active_targets(self.root, explicit_target=["claude", "nonexistent"])
        assert [t.name for t in targets] == ["claude"]

    def test_explicit_list_overrides_detection(self):
        """Explicit list wins even if dirs for other targets exist."""
        (self.root / ".github").mkdir()
        (self.root / ".claude").mkdir()
        targets = active_targets(self.root, explicit_target=["cursor"])
        assert [t.name for t in targets] == ["cursor"]

    def test_explicit_list_agents_alias(self):
        targets = active_targets(self.root, explicit_target=["agents", "claude"])
        assert [t.name for t in targets] == ["copilot", "claude"]

    def test_explicit_empty_list_falls_through_to_autodetect(self):
        """Empty list is falsy -- should auto-detect (fallback to copilot)."""
        targets = active_targets(self.root, explicit_target=[])
        assert [t.name for t in targets] == ["copilot"]  # fallback

    def test_explicit_list_preserves_order(self):
        """Result order matches input order."""
        targets = active_targets(self.root, explicit_target=["cursor", "claude", "copilot"])
        assert [t.name for t in targets] == ["cursor", "claude", "copilot"]

    def test_explicit_list_codex_at_project_scope(self):
        targets = active_targets(self.root, explicit_target=["codex"])
        assert [t.name for t in targets] == ["codex"]

    def test_copilot_profile_lists_root_generated_file(self):
        profile = KNOWN_TARGETS["copilot"]
        assert "copilot-instructions.md" in profile.generated_files


# ---------------------------------------------------------------------------
# Skill routing convergence (convergence §1)
# ---------------------------------------------------------------------------


class TestDefaultSkillRouting:
    """Assert that the 4 documented clients route skills to .agents/ by default."""

    def test_default_skill_routing_uses_agents_dir_for_documented_clients(self):
        """copilot, cursor, opencode, codex, gemini all have deploy_root='.agents' on skills."""
        expected = {
            "copilot": ".agents",
            "cursor": ".agents",
            "opencode": ".agents",
            "codex": ".agents",
            "gemini": ".agents",
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
            KNOWN_TARGETS[n] for n in ("copilot", "cursor", "opencode", "codex", "claude", "gemini")
        ]
        restored = apply_legacy_skill_paths(profiles)

        # All 6 should have deploy_root=None after legacy restore
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

    def test_apply_legacy_does_not_mutate_known_targets(self):
        """apply_legacy_skill_paths must not mutate the global KNOWN_TARGETS."""
        from apm_cli.integration.targets import apply_legacy_skill_paths

        original_root = KNOWN_TARGETS["copilot"].primitives["skills"].deploy_root
        profiles = [KNOWN_TARGETS["copilot"]]
        apply_legacy_skill_paths(profiles)
        assert KNOWN_TARGETS["copilot"].primitives["skills"].deploy_root == original_root
