"""Tests for target detection module."""

import contextlib

import click
import pytest

from apm_cli.core.target_detection import (
    ALL_CANONICAL_TARGETS,
    EXPERIMENTAL_TARGETS,
    VALID_TARGET_VALUES,
    TargetParamType,
    detect_target,
    get_target_description,
    normalize_target_list,
    should_compile_agents_md,
    should_compile_claude_md,
    should_compile_copilot_instructions_md,
    should_compile_gemini_md,
)


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


class TestShouldCompileCopilotInstructionsMd:
    """Tests for Copilot root instruction compilation routing."""

    def test_vscode_target(self):
        assert should_compile_copilot_instructions_md("vscode") is True

    def test_all_target(self):
        assert should_compile_copilot_instructions_md("all") is True

    def test_minimal_target(self):
        assert should_compile_copilot_instructions_md("minimal") is False

    def test_claude_target(self):
        assert should_compile_copilot_instructions_md("claude") is False

    def test_frozenset_with_vscode_returns_true(self):
        """Multi-target lists containing 'vscode' family member must emit."""
        assert (
            should_compile_copilot_instructions_md(frozenset({"vscode", "agents", "claude"}))
            is True
        )

    def test_frozenset_with_agents_only_returns_false(self):
        """Multi-target lists that map cursor/opencode/codex to 'agents'
        family for AGENTS.md routing must NOT trigger copilot-instructions.md.

        This is the round-3 regression: previously the predicate checked
        '"agents" in target' which over-fired on cursor/opencode/codex combos.
        """
        assert should_compile_copilot_instructions_md(frozenset({"agents", "claude"})) is False
        assert should_compile_copilot_instructions_md(frozenset({"agents"})) is False

    def test_frozenset_without_vscode_returns_false(self):
        """Multi-target lists without 'vscode' family must not emit."""
        assert should_compile_copilot_instructions_md(frozenset({"claude", "gemini"})) is False
        assert should_compile_copilot_instructions_md(frozenset({"claude"})) is False


class TestGetTargetDescription:
    """Tests for get_target_description function."""

    def test_copilot_description(self):
        """Description for copilot target."""
        desc = get_target_description("copilot")
        assert "AGENTS.md" in desc
        assert ".github/copilot-instructions.md" in desc
        assert ".github/" in desc

    def test_vscode_description(self):
        """Description for vscode target."""
        desc = get_target_description("vscode")
        assert "AGENTS.md" in desc
        assert ".github/copilot-instructions.md" in desc
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
        assert ".github/copilot-instructions.md" in desc

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


class TestDetectTargetWindsurf:
    """Tests for auto-detection and explicit windsurf target."""

    def test_explicit_target_windsurf(self, tmp_path):
        """Explicit --target windsurf always wins."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target="windsurf",
        )
        assert target == "windsurf"
        assert reason == "explicit --target flag"

    def test_config_target_windsurf(self, tmp_path):
        """Config target windsurf is used when no explicit target."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target="windsurf",
        )
        assert target == "windsurf"
        assert reason == "apm.yml target"

    def test_auto_detect_windsurf_only(self, tmp_path):
        """Auto-detect windsurf when only .windsurf/ exists."""
        (tmp_path / ".windsurf").mkdir()
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )
        assert target == "windsurf"
        assert ".windsurf/" in reason

    def test_auto_detect_windsurf_plus_github(self, tmp_path):
        """Auto-detect all when .windsurf/ and .github/ exist."""
        (tmp_path / ".github").mkdir()
        (tmp_path / ".windsurf").mkdir()
        target, _ = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )
        assert target == "all"

    def test_windsurf_compile_agents_md(self):
        """Windsurf target should compile AGENTS.md (reads it natively)."""
        assert should_compile_agents_md("windsurf") is True

    def test_windsurf_no_compile_claude_md(self):
        """Windsurf target should NOT compile CLAUDE.md."""
        assert should_compile_claude_md("windsurf") is False

    def test_windsurf_no_compile_gemini_md(self):
        """Windsurf target should NOT compile GEMINI.md."""
        assert should_compile_gemini_md("windsurf") is False

    def test_windsurf_description(self):
        """Description for windsurf target."""
        desc = get_target_description("windsurf")
        assert "AGENTS.md" in desc
        assert ".windsurf/" in desc

    def test_windsurf_in_all_canonical_targets(self):
        """Windsurf must appear in ALL_CANONICAL_TARGETS."""
        assert "windsurf" in ALL_CANONICAL_TARGETS

    def test_windsurf_in_valid_target_values(self):
        """Windsurf must be accepted by the --target parser."""
        assert "windsurf" in VALID_TARGET_VALUES


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
        """VALID_TARGET_VALUES contains user-facing aliases and explicit-only targets."""
        for name in ("copilot", "agents"):
            assert name in VALID_TARGET_VALUES
        assert "agent-skills" in VALID_TARGET_VALUES

    def test_valid_target_values_includes_all(self):
        """VALID_TARGET_VALUES contains 'all'."""
        assert "all" in VALID_TARGET_VALUES

    # -- None passthrough -------------------------------------------------

    def test_none_returns_none(self):
        """None value passes through unchanged."""
        assert self.tp.convert(None, None, None) is None

    # -- List input goes through the same validator as strings -----------

    def test_list_input_is_validated(self):
        """List input flows through parse_target_field: validated + deduped.

        Returned list is a fresh canonical sequence, not the input list --
        identity is no longer preserved because list and string inputs share
        a single normalization path.
        """
        result = self.tp.convert(["claude", "vscode"], None, None)
        assert result == ["claude", "vscode"]

    def test_list_input_collapses_aliases_to_string(self):
        """Multi-element list whose entries all alias to one canonical
        target collapses to that single canonical name (``"vscode"``)."""
        with pytest.warns(DeprecationWarning, match="--target agents"):
            assert self.tp.convert(["copilot", "agents"], None, None) == "vscode"

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
        with pytest.warns(DeprecationWarning, match="--target agents"):
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
        with pytest.warns(DeprecationWarning, match="--target agents"):
            result = self.tp.convert("copilot,agents", None, None)
        assert result == "vscode"

    def test_copilot_agents_vscode_deduplicates(self):
        """copilot,agents,vscode → 'vscode' (all alias to same)."""
        with pytest.warns(DeprecationWarning, match="--target agents"):
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
        with pytest.raises(click.UsageError, match="Unknown target"):
            self.tp.convert("invalid", None, None)

    def test_invalid_in_multi(self):
        """Invalid target in comma list produces clean error."""
        with pytest.raises(click.UsageError, match="Unknown target"):
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

    # -- agent-skills target + deprecation warning behaviour (#737) -------

    def test_explicit_only_targets_subset_of_known_targets(self):
        """EXPLICIT_ONLY_TARGETS is a subset of KNOWN_TARGETS keys."""
        from apm_cli.core.target_detection import EXPLICIT_ONLY_TARGETS
        from apm_cli.integration.targets import KNOWN_TARGETS

        assert frozenset(KNOWN_TARGETS) >= EXPLICIT_ONLY_TARGETS

    def test_agents_deprecation_fires_once_not_per_token(self):
        """parse_target_field('agents,agents') emits exactly one AgentsTargetDeprecationWarning."""
        import warnings

        from apm_cli.core.target_detection import (
            AgentsTargetDeprecationWarning,
            parse_target_field,
        )

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            parse_target_field("agents,agents")
            deprecation_warnings = [
                x for x in w if issubclass(x.category, AgentsTargetDeprecationWarning)
            ]
            assert len(deprecation_warnings) == 1

    def test_agents_deprecation_fires_for_apm_yml_target(self):
        """apm.yml target: agents path emits AgentsTargetDeprecationWarning."""
        import warnings

        from apm_cli.core.target_detection import (
            AgentsTargetDeprecationWarning,
            parse_target_field,
        )

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            parse_target_field("agents")
            deprecation_warnings = [
                x for x in w if issubclass(x.category, AgentsTargetDeprecationWarning)
            ]
            assert len(deprecation_warnings) == 1

    def test_agent_skills_does_not_emit_deprecation(self):
        """--target agent-skills does not emit DeprecationWarning."""
        import warnings

        from apm_cli.core.target_detection import parse_target_field

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            parse_target_field("agent-skills")
            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecation_warnings) == 0

    # -- F5: agents_alias_was_detected() tracks raw tokens across shapes ----

    @pytest.mark.parametrize(
        "raw_input",
        [
            "agents",
            "copilot,agents",
            "agents,claude",
            "all,agents",
        ],
        ids=["solo-agents", "copilot-comma-agents", "agents-comma-claude", "all-comma-agents"],
    )
    def test_agents_alias_detected_across_invocation_shapes(self, raw_input: str):
        """agents_alias_was_detected() returns True for all shapes containing 'agents'.

        Note: ``all,agents`` is rejected by parse_target_field (agents is a
        canonical alias, not an explicit-only target), but the flag is set
        *before* the ``all`` validation fires.
        """
        import warnings

        from apm_cli.core.target_detection import (
            agents_alias_was_detected,
            parse_target_field,
        )

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            with contextlib.suppress(ValueError):
                parse_target_field(raw_input)
                # "all,agents" raises; flag should still be set

        assert agents_alias_was_detected(), (
            f"agents_alias_was_detected() should be True for input {raw_input!r}"
        )

    def test_agents_alias_not_detected_for_copilot(self):
        """agents_alias_was_detected() returns False when 'agents' is absent."""
        import warnings

        from apm_cli.core.target_detection import (
            agents_alias_was_detected,
            parse_target_field,
        )

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            parse_target_field("copilot")

        assert not agents_alias_was_detected()

    # -- B1: detect_target() returns agent-skills for explicit --target ----

    def test_explicit_target_agent_skills(self):
        """detect_target(explicit_target='agent-skills') returns 'agent-skills'."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".github").mkdir()
            target, reason = detect_target(root, explicit_target="agent-skills")
            assert target == "agent-skills"
            assert reason == "explicit --target flag"

    def test_config_target_agent_skills(self):
        """detect_target(config_target='agent-skills') returns 'agent-skills'."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target, reason = detect_target(root, config_target="agent-skills")
            assert target == "agent-skills"
            assert reason == "apm.yml target"

    # -- B2: 'all,agent-skills' is allowed; 'all,claude' still rejected ----

    def test_all_combined_with_agent_skills_allowed(self):
        """'all,agent-skills' expands to every canonical target + agent-skills."""
        from apm_cli.core.target_detection import parse_target_field

        result = parse_target_field("all,agent-skills")
        assert isinstance(result, list)
        for t in ALL_CANONICAL_TARGETS:
            assert t in result, f"expected '{t}' in expansion, got {result}"
        assert "agent-skills" in result

    def test_all_combined_with_codex_still_rejected(self):
        """'all,codex' is still rejected (non-explicit-only combo)."""
        with pytest.raises(click.exceptions.BadParameter, match="cannot be combined"):
            self.tp.convert("all,codex", None, None)


# ---------------------------------------------------------------------------
# Cowork parser-layer regression tests (2f96dd5 / #926)
# ---------------------------------------------------------------------------


class TestCoworkParserLayer:
    """Regression guard for the parser-level EXPERIMENTAL_TARGETS fix.

    These tests are DELIBERATELY flag-agnostic -- the parser accepts or
    rejects tokens based solely on VALID_TARGET_VALUES, independent of
    the experimental flag state in ~/.apm/config.json.

    Ref: commit 2f96dd5 -- fix(cli): accept cowork target at parser layer
    via EXPERIMENTAL_TARGETS.
    """

    def setup_method(self):
        self.tp = TargetParamType()

    # -- Case 1: single "copilot-cowork" accepted ---------------------------------

    def test_convert_cowork_single_returns_string(self):
        """TargetParamType.convert('copilot-cowork') returns the string 'copilot-cowork'."""
        result = self.tp.convert("copilot-cowork", None, None)
        assert result == "copilot-cowork"
        assert isinstance(result, str)

    # -- Case 2: "copilot-cowork,claude" accepted as multi-target list -----------

    def test_convert_cowork_multi_returns_list_with_both(self):
        """TargetParamType.convert('copilot-cowork,claude') returns a list containing both."""
        result = self.tp.convert("copilot-cowork,claude", None, None)
        assert isinstance(result, list)
        assert "copilot-cowork" in result
        assert "claude" in result

    def test_convert_cowork_multi_preserves_input_order(self):
        """'copilot-cowork,claude' preserves the parser's natural (input) order."""
        result = self.tp.convert("copilot-cowork,claude", None, None)
        assert result == ["copilot-cowork", "claude"]

    # -- Case 3: membership in VALID_TARGET_VALUES -----------------------

    def test_cowork_in_valid_target_values(self):
        """'copilot-cowork' must be accepted by the --target parser."""
        assert "copilot-cowork" in VALID_TARGET_VALUES

    # -- Case 4: NOT in ALL_CANONICAL_TARGETS (constant-split guard) -----

    def test_cowork_not_in_all_canonical_targets(self):
        """'copilot-cowork' must NOT bleed into ALL_CANONICAL_TARGETS (regression guard).

        ALL_CANONICAL_TARGETS drives the 'all' expansion at the parser layer.
        Experimental targets are opt-in only and must live in EXPERIMENTAL_TARGETS.
        """
        assert "copilot-cowork" not in ALL_CANONICAL_TARGETS

    # -- Case 5: in EXPERIMENTAL_TARGETS --------------------------------

    def test_cowork_in_experimental_targets(self):
        """'copilot-cowork' must appear in EXPERIMENTAL_TARGETS."""
        assert "copilot-cowork" in EXPERIMENTAL_TARGETS

    # -- Case 6: exact membership lock -----------------------------------

    def test_experimental_targets_exact_membership(self):
        """EXPERIMENTAL_TARGETS must equal frozenset({'copilot-cowork'}) exactly.

        This locks the constant so that adding a new experimental target
        requires an intentional test update.
        """
        assert frozenset({"copilot-cowork"}) == EXPERIMENTAL_TARGETS

    # -- Case 7: "all" expansion does NOT include "copilot-cowork" ---------------

    def test_all_expansion_excludes_cowork(self):
        """parse_target_arg('all') at the parser layer must NOT include 'copilot-cowork'.

        'all' must expand only to ALL_CANONICAL_TARGETS.  Experimental
        targets are explicitly excluded -- they require opt-in.
        """
        # TargetParamType.convert("all") returns the string "all" for
        # backward compat.  The expansion to a list happens in
        # normalize_target_list(); test both surfaces.
        result_str = self.tp.convert("all", None, None)
        assert result_str == "all"

        result_list = normalize_target_list("all")
        assert isinstance(result_list, list)
        assert "copilot-cowork" not in result_list

    # -- Case 8: invalid target still rejected (sanity check) ------------

    def test_invalid_target_still_rejected(self):
        """'nonsense' must still raise UsageError after adding copilot-cowork."""
        with pytest.raises(
            click.UsageError,
            match="Unknown target",
        ):
            self.tp.convert("nonsense", None, None)
