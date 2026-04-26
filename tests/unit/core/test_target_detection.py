"""Tests for target detection module."""

from apm_cli.core.target_detection import (
    detect_target,
    should_compile_agents_md,
    should_compile_claude_md,
    should_compile_gemini_md,
    get_target_description,
    TargetParamType,
    VALID_TARGET_VALUES,
)

import click
import pytest


class TestDetectTarget:
    """Tests for detect_target function."""

    def test_explicit_target_vscode_wins(self, tmp_path):
        """Explicit --target vscode always wins."""
        # Create both folders - should still use explicit
        (tmp_path / ".github").mkdir()
        (tmp_path / ".claude").mkdir()
        
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target="vscode",
            config_target="claude",
        )
        
        assert target == "vscode"
        assert reason == "explicit --target flag"

    def test_explicit_target_copilot_maps_to_vscode(self, tmp_path):
        """Explicit --target copilot maps to vscode."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target="copilot",
        )
        
        assert target == "vscode"
        assert reason == "explicit --target flag"

    def test_explicit_target_agents_maps_to_vscode(self, tmp_path):
        """Explicit --target agents maps to vscode."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target="agents",
        )
        
        assert target == "vscode"
        assert reason == "explicit --target flag"

    def test_explicit_target_claude_wins(self, tmp_path):
        """Explicit --target claude always wins."""
        (tmp_path / ".github").mkdir()
        
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target="claude",
        )
        
        assert target == "claude"
        assert reason == "explicit --target flag"

    def test_explicit_target_all_wins(self, tmp_path):
        """Explicit --target all always wins."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target="all",
        )
        
        assert target == "all"
        assert reason == "explicit --target flag"

    def test_config_target_copilot(self, tmp_path):
        """Config target copilot maps to vscode."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target="copilot",
        )
        
        assert target == "vscode"
        assert reason == "apm.yml target"

    def test_config_target_vscode(self, tmp_path):
        """Config target vscode is used when no explicit target."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target="vscode",
        )
        
        assert target == "vscode"
        assert reason == "apm.yml target"

    def test_config_target_claude(self, tmp_path):
        """Config target claude is used when no explicit target."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target="claude",
        )
        
        assert target == "claude"
        assert reason == "apm.yml target"

    def test_config_target_all(self, tmp_path):
        """Config target all is used when no explicit target."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target="all",
        )
        
        assert target == "all"
        assert reason == "apm.yml target"

    def test_auto_detect_github_only(self, tmp_path):
        """Auto-detect vscode when only .github/ exists."""
        (tmp_path / ".github").mkdir()
        
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )
        
        assert target == "vscode"
        assert "detected .github/ folder" in reason

    def test_auto_detect_claude_only(self, tmp_path):
        """Auto-detect claude when only .claude/ exists."""
        (tmp_path / ".claude").mkdir()
        
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )
        
        assert target == "claude"
        assert "detected .claude/ folder" in reason

    def test_auto_detect_both_folders(self, tmp_path):
        """Auto-detect all when both folders exist."""
        (tmp_path / ".github").mkdir()
        (tmp_path / ".claude").mkdir()
        
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )
        
        assert target == "all"
        assert ".github/" in reason and ".claude/" in reason

    def test_auto_detect_neither_folder(self, tmp_path):
        """Auto-detect minimal when neither folder exists."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )
        
        assert target == "minimal"
        assert "no target folder found" in reason


class TestShouldCompileAgentsMd:
    """Tests for should_compile_agents_md function."""

    def test_vscode_target(self):
        """AGENTS.md compiled for vscode target."""
        assert should_compile_agents_md("vscode") is True

    def test_all_target(self):
        """AGENTS.md compiled for all target."""
        assert should_compile_agents_md("all") is True

    def test_minimal_target(self):
        """AGENTS.md compiled for minimal target (universal format)."""
        assert should_compile_agents_md("minimal") is True

    def test_claude_target(self):
        """AGENTS.md not compiled for claude target."""
        assert should_compile_agents_md("claude") is False

    def test_gemini_target(self):
        """AGENTS.md compiled for gemini target (GEMINI.md imports it)."""
        assert should_compile_agents_md("gemini") is True


class TestShouldCompileClaudeMd:
    """Tests for should_compile_claude_md function."""

    def test_claude_target(self):
        """CLAUDE.md compiled for claude target."""
        assert should_compile_claude_md("claude") is True

    def test_all_target(self):
        """CLAUDE.md compiled for all target."""
        assert should_compile_claude_md("all") is True

    def test_vscode_target(self):
        """CLAUDE.md not compiled for vscode target."""
        assert should_compile_claude_md("vscode") is False

    def test_minimal_target(self):
        """CLAUDE.md not compiled for minimal target."""
        assert should_compile_claude_md("minimal") is False


class TestShouldCompileGeminiMd:
    """Tests for should_compile_gemini_md function."""

    def test_gemini_target_returns_true(self):
        """GEMINI.md compiled for gemini target."""
        assert should_compile_gemini_md("gemini") is True

    def test_all_target_returns_true(self):
        """GEMINI.md compiled for all target."""
        assert should_compile_gemini_md("all") is True

    def test_claude_target_returns_false(self):
        """GEMINI.md not compiled for claude target."""
        assert should_compile_gemini_md("claude") is False

    def test_vscode_target_returns_false(self):
        """GEMINI.md not compiled for vscode target."""
        assert should_compile_gemini_md("vscode") is False

    def test_codex_target_returns_false(self):
        """GEMINI.md not compiled for codex target."""
        assert should_compile_gemini_md("codex") is False

    def test_minimal_target_returns_false(self):
        """GEMINI.md not compiled for minimal target."""
        assert should_compile_gemini_md("minimal") is False


class TestGetTargetDescription:
    """Tests for get_target_description function."""

    def test_copilot_description(self):
        """Description for copilot target."""
        desc = get_target_description("copilot")
        assert "AGENTS.md" in desc
        assert ".github/" in desc

    def test_vscode_description(self):
        """Description for vscode target."""
        desc = get_target_description("vscode")
        assert "AGENTS.md" in desc
        assert ".github/" in desc

    def test_claude_description(self):
        """Description for claude target."""
        desc = get_target_description("claude")
        assert "CLAUDE.md" in desc
        assert ".claude/" in desc

    def test_all_description(self):
        """Description for all target."""
        desc = get_target_description("all")
        assert "AGENTS.md" in desc
        assert "CLAUDE.md" in desc

    def test_minimal_description(self):
        """Description for minimal target."""
        desc = get_target_description("minimal")
        assert "AGENTS.md only" in desc

    def test_opencode_description(self):
        """Description for opencode target."""
        desc = get_target_description("opencode")
        assert "AGENTS.md" in desc
        assert ".opencode/" in desc


class TestDetectTargetCursor:
    """Tests for auto-detection and explicit cursor target."""

    def test_explicit_target_cursor(self, tmp_path):
        """Explicit --target cursor always wins."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target="cursor",
        )
        assert target == "cursor"
        assert reason == "explicit --target flag"

    def test_config_target_cursor(self, tmp_path):
        """Config target cursor is used when no explicit target."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target="cursor",
        )
        assert target == "cursor"
        assert reason == "apm.yml target"

    def test_auto_detect_cursor_only(self, tmp_path):
        """Auto-detect cursor when only .cursor/ exists."""
        (tmp_path / ".cursor").mkdir()
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )
        assert target == "cursor"
        assert ".cursor/" in reason

    def test_auto_detect_cursor_plus_github(self, tmp_path):
        """Auto-detect all when .cursor/ and .github/ exist."""
        (tmp_path / ".github").mkdir()
        (tmp_path / ".cursor").mkdir()
        target, _ = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )
        assert target == "all"

    def test_cursor_no_compile_agents_md(self):
        """Cursor target should NOT compile AGENTS.md (uses .cursor/agents/)."""
        assert should_compile_agents_md("cursor") is False

    def test_cursor_no_compile_claude_md(self):
        """Cursor target should NOT compile CLAUDE.md."""
        assert should_compile_claude_md("cursor") is False

    def test_cursor_description(self):
        """Description for cursor target."""
        desc = get_target_description("cursor")
        assert ".cursor/" in desc


class TestDetectTargetOpencode:
    """Tests for auto-detection of OpenCode folders."""

    def test_auto_detect_opencode_only(self, tmp_path):
        """Auto-detect opencode when only .opencode/ exists."""
        (tmp_path / ".opencode").mkdir()
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )
        assert target == "opencode"
        assert ".opencode/" in reason

    def test_auto_detect_opencode_plus_github(self, tmp_path):
        """Auto-detect all when .opencode/ and .github/ exist."""
        (tmp_path / ".github").mkdir()
        (tmp_path / ".opencode").mkdir()
        target, _ = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )
        assert target == "all"

    def test_opencode_compile_agents_md(self):
        """OpenCode target should compile AGENTS.md."""
        assert should_compile_agents_md("opencode") is True

    def test_opencode_no_compile_claude_md(self):
        """OpenCode target should NOT compile CLAUDE.md."""
        assert should_compile_claude_md("opencode") is False


# ---------------------------------------------------------------------------
# TargetParamType tests
# ---------------------------------------------------------------------------

class TestTargetParamType:
    """Tests for TargetParamType Click parameter type."""

    def setup_method(self):
        self.tp = TargetParamType()

    # -- Valid target values set ------------------------------------------

    def test_valid_target_values_includes_canonical(self):
        """VALID_TARGET_VALUES contains all canonical targets."""
        for name in ("vscode", "claude", "cursor", "opencode", "codex"):
            assert name in VALID_TARGET_VALUES

    def test_valid_target_values_includes_aliases(self):
        """VALID_TARGET_VALUES contains user-facing aliases."""
        for name in ("copilot", "agents"):
            assert name in VALID_TARGET_VALUES

    def test_valid_target_values_includes_all(self):
        """VALID_TARGET_VALUES contains 'all'."""
        assert "all" in VALID_TARGET_VALUES

    # -- None passthrough -------------------------------------------------

    def test_none_returns_none(self):
        """None value passes through unchanged."""
        assert self.tp.convert(None, None, None) is None

    # -- Already-converted list passthrough -------------------------------

    def test_list_passthrough(self):
        """A list value passes through unchanged."""
        lst = ["claude", "vscode"]
        assert self.tp.convert(lst, None, None) is lst

    # -- Single target (backward compat: returns string) ------------------

    def test_single_claude(self):
        assert self.tp.convert("claude", None, None) == "claude"

    def test_single_copilot(self):
        assert self.tp.convert("copilot", None, None) == "copilot"

    def test_single_vscode(self):
        assert self.tp.convert("vscode", None, None) == "vscode"

    def test_single_cursor(self):
        assert self.tp.convert("cursor", None, None) == "cursor"

    def test_single_opencode(self):
        assert self.tp.convert("opencode", None, None) == "opencode"

    def test_single_codex(self):
        assert self.tp.convert("codex", None, None) == "codex"

    def test_single_agents(self):
        assert self.tp.convert("agents", None, None) == "agents"

    def test_single_all(self):
        """'all' returns string 'all' for backward compat."""
        assert self.tp.convert("all", None, None) == "all"

    def test_single_target_returns_string_type(self):
        """Single target must return str, not list."""
        result = self.tp.convert("claude", None, None)
        assert isinstance(result, str)

    # -- Case insensitivity -----------------------------------------------

    def test_uppercase_accepted(self):
        assert self.tp.convert("CLAUDE", None, None) == "claude"

    def test_mixed_case_accepted(self):
        assert self.tp.convert("Claude", None, None) == "claude"

    def test_mixed_case_multi(self):
        result = self.tp.convert("Claude,Copilot", None, None)
        assert result == ["claude", "vscode"]

    # -- Multi-target (returns list) --------------------------------------

    def test_multi_claude_copilot(self):
        """claude,copilot → ['claude', 'vscode'] (alias resolved)."""
        result = self.tp.convert("claude,copilot", None, None)
        assert result == ["claude", "vscode"]

    def test_multi_preserves_order(self):
        """Order of user input is preserved."""
        result = self.tp.convert("cursor,claude", None, None)
        assert result == ["cursor", "claude"]

    def test_multi_returns_list_type(self):
        """Multi-target must return list, not str."""
        result = self.tp.convert("claude,cursor", None, None)
        assert isinstance(result, list)

    def test_multi_three_targets(self):
        result = self.tp.convert("claude,cursor,codex", None, None)
        assert result == ["claude", "cursor", "codex"]

    # -- Alias deduplication ----------------------------------------------

    def test_copilot_vscode_deduplicates(self):
        """copilot,vscode → 'vscode' (both alias to same canonical)."""
        result = self.tp.convert("copilot,vscode", None, None)
        # Both map to "vscode"; collapses to single string.
        assert result == "vscode"

    def test_copilot_agents_deduplicates(self):
        """copilot,agents → 'vscode' (both alias to same canonical)."""
        result = self.tp.convert("copilot,agents", None, None)
        assert result == "vscode"

    def test_copilot_agents_vscode_deduplicates(self):
        """copilot,agents,vscode → 'vscode' (all alias to same)."""
        result = self.tp.convert("copilot,agents,vscode", None, None)
        assert result == "vscode"

    def test_copilot_claude_deduplicates_alias(self):
        """copilot,claude → ['vscode', 'claude'] (alias resolved)."""
        result = self.tp.convert("copilot,claude", None, None)
        assert result == ["vscode", "claude"]

    # -- Whitespace and formatting ----------------------------------------

    def test_spaces_around_comma(self):
        result = self.tp.convert("claude , copilot", None, None)
        assert result == ["claude", "vscode"]

    def test_trailing_comma_ignored(self):
        result = self.tp.convert("claude,", None, None)
        assert result == "claude"

    def test_leading_comma_ignored(self):
        result = self.tp.convert(",claude", None, None)
        assert result == "claude"

    def test_double_comma_ignored(self):
        result = self.tp.convert("claude,,cursor", None, None)
        assert result == ["claude", "cursor"]

    # -- Error cases ------------------------------------------------------

    def test_invalid_single_target(self):
        """Invalid target name produces clean error."""
        with pytest.raises(click.exceptions.BadParameter, match="'invalid' is not a valid target"):
            self.tp.convert("invalid", None, None)

    def test_invalid_in_multi(self):
        """Invalid target in comma list produces clean error."""
        with pytest.raises(click.exceptions.BadParameter, match="'nope' is not a valid target"):
            self.tp.convert("claude,nope", None, None)

    def test_all_combined_with_other_rejected(self):
        """'all' combined with other targets is rejected."""
        with pytest.raises(click.exceptions.BadParameter, match="cannot be combined"):
            self.tp.convert("all,claude", None, None)

    def test_target_combined_with_all_rejected(self):
        """Target followed by 'all' is also rejected."""
        with pytest.raises(click.exceptions.BadParameter, match="cannot be combined"):
            self.tp.convert("claude,all", None, None)

    def test_empty_string_rejected(self):
        """Empty string is rejected."""
        with pytest.raises(click.exceptions.BadParameter, match="must not be empty"):
            self.tp.convert("", None, None)

    def test_only_commas_rejected(self):
        """Only commas (no actual values) is rejected."""
        with pytest.raises(click.exceptions.BadParameter, match="must not be empty"):
            self.tp.convert(",,,", None, None)
