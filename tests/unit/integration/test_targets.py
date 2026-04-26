"""Tests for active_targets() resolution in targets.py."""

import tempfile
import shutil
from pathlib import Path

from apm_cli.integration.targets import active_targets, KNOWN_TARGETS


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
        targets = active_targets(self.root, explicit_target="all")
        assert len(targets) == len(KNOWN_TARGETS)

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

    def test_explicit_unknown_returns_empty(self):
        targets = active_targets(self.root, explicit_target="nonexistent")
        assert targets == []

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

    def test_all_six_dirs_returns_all_six(self):
        for d in (".github", ".claude", ".cursor", ".opencode", ".codex", ".gemini"):
            (self.root / d).mkdir()
        targets = active_targets(self.root)
        assert len(targets) == 6

    def test_all_five_dirs_returns_all_five(self):
        for d in (".github", ".claude", ".cursor", ".opencode", ".codex"):
            (self.root / d).mkdir()
        targets = active_targets(self.root)
        assert len(targets) == 5

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
        targets = active_targets(self.root, explicit_target=["all"])
        assert len(targets) == len(KNOWN_TARGETS)

    def test_explicit_list_all_mixed_returns_every_known_target(self):
        """'all' anywhere in the list wins."""
        targets = active_targets(self.root, explicit_target=["claude", "all"])
        assert len(targets) == len(KNOWN_TARGETS)

    def test_explicit_list_unknown_targets_falls_back_to_copilot(self):
        targets = active_targets(self.root, explicit_target=["nonexistent", "bogus"])
        assert [t.name for t in targets] == ["copilot"]

    def test_explicit_list_mixed_known_unknown(self):
        """Known targets are included, unknown ones are silently skipped."""
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
        targets = active_targets(
            self.root, explicit_target=["cursor", "claude", "copilot"]
        )
        assert [t.name for t in targets] == ["cursor", "claude", "copilot"]

    def test_explicit_list_codex_at_project_scope(self):
        targets = active_targets(self.root, explicit_target=["codex"])
        assert [t.name for t in targets] == ["codex"]
