"""Tests for target detection module (pure logic cases)."""

import click
import pytest

from apm_cli.core import target_detection
from apm_cli.core.target_detection import (
    ALL_CANONICAL_TARGETS,
    EXPERIMENTAL_TARGETS,
    MCP_ONLY_TARGETS,
    VALID_TARGET_VALUES,
    EffectiveTargetDecision,
    TargetParamType,
    can_dedup_agents_md_instructions,
    canonical_policy_target_name,
    canonical_policy_target_names,
    get_dedup_rules_dir,
    get_target_description,
    normalize_target_list,
    should_compile_agents_md,
    should_compile_claude_md,
    should_compile_copilot_instructions_md,
    should_compile_gemini_md,
)

pytestmark = pytest.mark.unit


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

    def test_kiro_target(self):
        """AGENTS.md compiled for kiro as a cross-harness fallback."""
        assert should_compile_agents_md("kiro") is True


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

    def test_project_skill_dirs_match_the_target_profiles(self):
        """Compile names the skills dir install deploys to, never a legacy one (#3994)."""
        import re

        from apm_cli.integration.targets import KNOWN_TARGETS

        for name, profile in KNOWN_TARGETS.items():
            if (
                "skills" not in profile.primitives
                or profile.primitives["skills"].subdir != "skills"
            ):
                continue
            try:
                desc = get_target_description(name)
            except KeyError:
                continue
            # Project paths only: '$HERMES_HOME/skills/' and '~/...' are global roots.
            named = set(re.findall(r"(?<![$~/\w])(\.[\w-]+/skills)/", desc))
            assert named <= {profile.skills_rel_root}, (name, named, profile.skills_rel_root)

    def test_kiro_description_includes_mcp_config_path(self):
        """Description for kiro target names its MCP config path."""
        desc = get_target_description("kiro")
        assert ".kiro/steering/" in desc
        assert ".kiro/skills/" in desc
        assert ".kiro/hooks/" in desc
        assert ".kiro/settings/mcp.json" in desc


# ---------------------------------------------------------------------------
# TargetParamType tests
# ---------------------------------------------------------------------------


class TestIntelliJConstantGuards:
    """Constant-split guards ensuring intellij stays MCP-only (#1957).

    IntelliJ is an MCP-only pseudo-target -- it must be in MCP_ONLY_TARGETS
    and VALID_TARGET_VALUES, but NOT in ALL_CANONICAL_TARGETS. The 'all'
    expansion must never include it.
    """

    def test_intellij_is_valid_mcp_only_target(self):
        """'intellij' is a valid --target value that lives in MCP_ONLY_TARGETS.

        ALL_CANONICAL_TARGETS drives the 'all' expansion, so IntelliJ must
        NOT appear there.
        """
        assert "intellij" in VALID_TARGET_VALUES
        assert "intellij" in MCP_ONLY_TARGETS
        assert "intellij" not in ALL_CANONICAL_TARGETS

    def test_all_expansion_excludes_intellij(self):
        """normalize_target_list('all') must NOT include 'intellij'.

        'all' expands only to ALL_CANONICAL_TARGETS. MCP-only targets
        require explicit opt-in via '--target intellij' or 'all,intellij'.
        """
        result = normalize_target_list("all")
        assert isinstance(result, list)
        assert "intellij" not in result
        # Verify all canonical targets ARE present
        for t in ALL_CANONICAL_TARGETS:
            assert t in result


class TestPolicyTargetProjection:
    """Contract tests for the compilation-target policy projection (#3732)."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("intellij", ("copilot",)),
            (["intellij", "claude"], ("copilot", "claude")),
            (["intellij", "copilot"], ("copilot",)),
            ("vscode", ("copilot",)),
            ("agents", ("copilot",)),
            ("agy", ("antigravity",)),
            (["claude", "vscode"], ("claude", "copilot")),
        ],
    )
    def test_decision_projects_aliases_and_mcp_only_targets(
        self,
        value: str | list[str] | None,
        expected: tuple[str, ...] | None,
    ) -> None:
        """The decision projection resolves aliases and MCP-only targets."""
        assert EffectiveTargetDecision(value, "apm.yml").canonical_targets == expected

    def test_decision_expands_all(self) -> None:
        """``all`` projects to every target an install deploys."""
        projected = EffectiveTargetDecision("all", "apm.yml").canonical_targets
        assert projected is not None
        assert "all" not in projected
        assert {"claude", "copilot"} <= set(projected)

    @pytest.mark.parametrize("copilot_spelling", ["copilot", "vscode"])
    def test_runtime_targets_never_yield_vscode(self, copilot_spelling: str) -> None:
        """Copilot projects to the copilot MCP runtime, never vscode (#3866)."""
        decision = EffectiveTargetDecision(["claude", copilot_spelling], "apm.yml")
        assert decision.canonical_targets == ("claude", "copilot")
        assert decision.runtime_targets == ("claude", "copilot")

    def test_unmapped_mcp_only_target_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reject an MCP-only target without a canonical policy mapping."""
        from dataclasses import replace

        real = target_detection.get_target_capability
        monkeypatch.setattr(
            target_detection,
            "get_target_capability",
            lambda name: replace(real(name), primitive_profile=None),
        )

        with pytest.raises(RuntimeError, match="has no canonical policy mapping"):
            _ = EffectiveTargetDecision("intellij", "apm.yml").canonical_targets

    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            (None, None),
            ((), ()),
            (("vscode", "claude"), ("copilot", "claude")),
            (("Copilot", "agents"), ("copilot",)),
            (("all",), None),
            (("claude", "all"), None),
            (("agy", "unknown-tool"), ("antigravity", "unknown-tool")),
        ],
    )
    def test_policy_names_canonicalize(
        self,
        values: tuple[str, ...] | None,
        expected: tuple[str, ...] | None,
    ) -> None:
        """Policy allow lists compare on profiles; ``all`` means unrestricted."""
        assert canonical_policy_target_names(values) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, None), ("vscode", "copilot"), ("all", "all"), ("claude", "claude")],
    )
    def test_policy_enforce_canonicalizes(self, value: str | None, expected: str | None) -> None:
        """A single enforced target resolves its alias; ``all`` is kept."""
        assert canonical_policy_target_name(value) == expected


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
        """EXPERIMENTAL_TARGETS must equal the expected set exactly.

        This locks the constant so that adding a new experimental target
        requires an intentional test update.
        """
        assert (
            frozenset({"copilot-cowork", "copilot-app", "grok-cloud", "openclaw"})
            == EXPERIMENTAL_TARGETS
        )

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


# ---------------------------------------------------------------------------
# can_dedup_agents_md_instructions (issue #1678)
# ---------------------------------------------------------------------------


class TestCanDedupAgentsMdInstructions:
    """Only targets with dedicated local rules directories (vscode, antigravity) allow instruction dedup from AGENTS.md."""

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            # Copilot reads both AGENTS.md and .github/instructions/ -- safe to dedup.
            ("vscode", True),
            # Antigravity reads both AGENTS.md and .agents/rules/ -- safe to dedup.
            ("antigravity", True),
            # Non-Copilot/Antigravity targets only read AGENTS.md -- must NOT dedup.
            ("codex", False),
            ("opencode", False),
            ("windsurf", False),
            ("gemini", False),
            ("all", False),
            ("minimal", False),
            ("claude", False),
            ("cursor", False),
            # Multi-target frozensets.
            (frozenset({"vscode"}), True),
            (frozenset({"vscode", "agents"}), False),
            (frozenset({"vscode", "claude"}), False),
            (frozenset({"agents"}), False),
            (frozenset({"vscode", "claude", "agents"}), False),
        ],
        ids=[
            "vscode-str",
            "antigravity-str",
            "codex-str",
            "opencode-str",
            "windsurf-str",
            "gemini-str",
            "all-str",
            "minimal-str",
            "claude-str",
            "cursor-str",
            "frozenset-vscode-only",
            "frozenset-vscode-agents",
            "frozenset-vscode-claude",
            "frozenset-agents-only",
            "frozenset-vscode-claude-agents",
        ],
    )
    def test_dedup_decision(self, target, expected):
        """can_dedup_agents_md_instructions returns expected value per target."""
        assert can_dedup_agents_md_instructions(target) is expected


# ---------------------------------------------------------------------------
# _resolve_compile_target regression tests (issue #1678)
# ---------------------------------------------------------------------------


class TestResolveCompileTargetMixedTargets:
    """_resolve_compile_target must preserve frozenset for mixed targets."""

    @staticmethod
    def _resolve(targets):
        from apm_cli.commands.compile.cli import _resolve_compile_target

        return _resolve_compile_target(targets)

    def test_copilot_only_list_collapses_to_vscode(self):
        """[copilot] collapses to bare 'vscode' string."""
        assert self._resolve(["copilot"]) == "vscode"

    def test_copilot_codex_keeps_frozenset(self):
        """[copilot, codex] must NOT collapse -- Codex info must survive."""
        result = self._resolve(["copilot", "codex"])
        assert isinstance(result, frozenset)
        assert "vscode" in result
        assert "agents" in result

    def test_copilot_opencode_keeps_frozenset(self):
        """[copilot, opencode] must NOT collapse."""
        result = self._resolve(["copilot", "opencode"])
        assert isinstance(result, frozenset)

    def test_copilot_windsurf_keeps_frozenset(self):
        """[copilot, windsurf] must NOT collapse."""
        result = self._resolve(["copilot", "windsurf"])
        assert isinstance(result, frozenset)

    def test_codex_single_string(self):
        """Single codex target stays a bare string."""
        assert self._resolve(["codex"]) == "codex"

    def test_antigravity_codex_keeps_agents_family_frozenset(self):
        """Mixed Antigravity+Codex must not collapse to Antigravity."""
        assert self._resolve(["antigravity", "codex"]) == frozenset({"agents"})

    def test_copilot_claude_keeps_frozenset(self):
        """[copilot, claude] produces a frozenset with vscode and claude families."""
        result = self._resolve(["copilot", "claude"])
        assert isinstance(result, frozenset)
        assert "vscode" in result
        assert "claude" in result


class TestGetDedupRulesDir:
    """Tests for get_dedup_rules_dir resolving canonical targets and aliases."""

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ("vscode", (".github/instructions", "copilot")),
            ("copilot", (".github/instructions", "copilot")),
            ("agents", (".github/instructions", "copilot")),
            ("antigravity", (".agents/rules", "antigravity")),
            ("agy", (".agents/rules", "antigravity")),
            ("claude", None),
            (frozenset({"vscode"}), (".github/instructions", "copilot")),
            (frozenset({"vscode", "agents"}), None),
        ],
    )
    def test_get_dedup_rules_dir(self, target, expected):
        assert get_dedup_rules_dir(target) == expected
