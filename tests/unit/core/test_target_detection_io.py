"""Tests for target detection module (filesystem, process and CLI boundary cases)."""

import pytest

from apm_cli.core.target_detection import (
    EXPERIMENTAL_TARGETS,
    TargetParamType,
    detect_target,
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


# ---------------------------------------------------------------------------
# TargetParamType tests
# ---------------------------------------------------------------------------


class TestTargetParamType:
    """Tests for TargetParamType Click parameter type."""

    def setup_method(self):
        self.tp = TargetParamType()

    # -- Valid target values set ------------------------------------------

    # -- None passthrough -------------------------------------------------

    # -- List input goes through the same validator as strings -----------

    # -- Single target (backward compat: returns string) ------------------

    # -- Case insensitivity -----------------------------------------------

    # -- Multi-target (returns list) --------------------------------------

    # -- Alias deduplication ----------------------------------------------

    # -- Whitespace and formatting ----------------------------------------

    # -- Error cases ------------------------------------------------------

    # -- agent-skills target + deprecation warning behaviour (#737) -------

    # -- F5: agents_alias_was_detected() tracks raw tokens across shapes ----

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
