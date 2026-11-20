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
    should_compile_agents_md,
    should_compile_claude_md,
    should_compile_gemini_md,
)

pytestmark = pytest.mark.component


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

    def test_explicit_target_intellij_maps_to_vscode(self, tmp_path):
        """Explicit --target intellij maps to vscode (MCP-only target, #1957)."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target="intellij",
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

    def test_explicit_target_kiro_wins(self, tmp_path):
        """Explicit --target kiro always wins."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target="kiro",
        )

        assert target == "kiro"
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

    def test_config_target_intellij(self, tmp_path):
        """Config target intellij maps to the Copilot deployment profile."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target="intellij",
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

    def test_auto_detect_kiro_only(self, tmp_path):
        """Auto-detect kiro when only .kiro/ exists."""
        (tmp_path / ".kiro").mkdir()

        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )

        assert target == "kiro"
        assert "detected .kiro/ folder" in reason

    def test_auto_detect_neither_folder(self, tmp_path):
        """Auto-detect minimal when neither folder exists."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )

        assert target == "minimal"
        assert "no target folder found" in reason


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

    def test_cursor_compiles_agents_md(self):
        """Cursor's agents compile family emits the shared root context."""
        assert should_compile_agents_md("cursor") is True

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
        for name in (
            "vscode",
            "claude",
            "cursor",
            "opencode",
            "codex",
            "grok-build",
            "grok-cloud",
        ):
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
        assert result == ["claude", "copilot"]

    def test_list_input_collapses_aliases_to_string(self):
        """Multi-element list whose entries all alias to one canonical
        target collapses to that single canonical name (``"copilot"``)."""
        with pytest.warns(DeprecationWarning, match="--target agents"):
            assert self.tp.convert(["copilot", "agents"], None, None) == "copilot"

    # -- Single target (backward compat: returns string) ------------------

    def test_single_claude(self):
        assert self.tp.convert("claude", None, None) == "claude"

    def test_single_copilot(self):
        assert self.tp.convert("copilot", None, None) == "copilot"

    def test_single_vscode(self):
        """The deprecated vscode alias always resolves to copilot (#3866)."""
        assert self.tp.convert("vscode", None, None) == "copilot"

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

    def test_single_intellij(self):
        """intellij is accepted as a valid MCP-only target (#1957)."""
        assert self.tp.convert("intellij", None, None) == "intellij"

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
        assert result == ["claude", "copilot"]

    # -- Multi-target (returns list) --------------------------------------

    def test_multi_claude_copilot(self):
        """claude,copilot preserves Copilot's canonical target spelling."""
        result = self.tp.convert("claude,copilot", None, None)
        assert result == ["claude", "copilot"]

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

    def test_multi_intellij_with_claude(self):
        """intellij,claude keeps intellij as-is in multi-target (#1957)."""
        result = self.tp.convert("intellij,claude", None, None)
        assert result == ["intellij", "claude"]

    # -- Alias deduplication ----------------------------------------------

    def test_copilot_vscode_deduplicates(self):
        """copilot,vscode collapse: vscode has no MCP destination of its own (#3866)."""
        result = self.tp.convert("copilot,vscode", None, None)
        assert result == "copilot"

    def test_copilot_agents_deduplicates(self):
        """copilot,agents collapse to Copilot's canonical target."""
        with pytest.warns(DeprecationWarning, match="--target agents"):
            result = self.tp.convert("copilot,agents", None, None)
        assert result == "copilot"

    def test_copilot_agents_vscode_deduplicates(self):
        """Both deprecated aliases collapse into copilot."""
        with pytest.warns(DeprecationWarning, match="--target agents"):
            result = self.tp.convert("copilot,agents,vscode", None, None)
        assert result == "copilot"

    def test_copilot_claude_deduplicates_alias(self):
        """copilot,claude preserves the configured target destinations."""
        result = self.tp.convert("copilot,claude", None, None)
        assert result == ["copilot", "claude"]

    # -- Whitespace and formatting ----------------------------------------

    def test_spaces_around_comma(self):
        result = self.tp.convert("claude , copilot", None, None)
        assert result == ["claude", "copilot"]

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

    @pytest.mark.parametrize("name", sorted(EXPERIMENTAL_TARGETS))
    def test_explicit_experimental_target_is_honored(self, name, tmp_path):
        """Experimental targets are not replaced by folder auto-detection (#3842)."""
        (tmp_path / ".claude").mkdir()
        assert detect_target(tmp_path, explicit_target=name) == (name, "explicit --target flag")

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

    def test_all_combined_with_intellij_allowed(self):
        """'all,intellij' is allowed -- intellij is an MCP-only target (#1957)."""
        from apm_cli.core.target_detection import parse_target_field

        result = parse_target_field("all,intellij")
        assert isinstance(result, list)
        for t in ALL_CANONICAL_TARGETS:
            assert t in result
        assert "intellij" in result


# ---------------------------------------------------------------------------
# Cowork parser-layer regression tests (2f96dd5 / #926)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# can_dedup_agents_md_instructions (issue #1678)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _resolve_compile_target regression tests (issue #1678)
# ---------------------------------------------------------------------------


class TestDetectTargetGrokBuild:
    """Tests for explicit, config, and auto-detection of grok-build target."""

    def test_explicit_target_grok_build(self, tmp_path):
        """Explicit --target grok-build always wins."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target="grok-build",
        )
        assert target == "grok-build"
        assert reason == "explicit --target flag"

    def test_config_target_grok_build(self, tmp_path):
        """Config target grok-build is used when no explicit target."""
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target="grok-build",
        )
        assert target == "grok-build"
        assert reason == "apm.yml target"

    def test_auto_detect_grok_build_only(self, tmp_path):
        """Auto-detect grok-build when only .grok/ exists."""
        (tmp_path / ".grok").mkdir()
        target, reason = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )
        assert target == "grok-build"
        assert "detected .grok/ folder" in reason

    def test_auto_detect_grok_plus_github(self, tmp_path):
        """Auto-detect all when .grok/ and .github/ both exist."""
        (tmp_path / ".github").mkdir()
        (tmp_path / ".grok").mkdir()
        target, _ = detect_target(
            project_root=tmp_path,
            explicit_target=None,
            config_target=None,
        )
        assert target == "all"

    def test_grok_build_compiles_agents_md(self):
        """grok-build target produces AGENTS.md root context."""
        assert should_compile_agents_md("grok-build") is True

    def test_grok_build_no_compile_claude_md(self):
        """grok-build target does NOT compile CLAUDE.md."""
        assert should_compile_claude_md("grok-build") is False

    def test_get_target_description_grok_build(self):
        """get_target_description returns grok-build specific paths."""
        desc = get_target_description("grok-build")
        assert ".grok/" in desc
        assert desc != "unknown target"
