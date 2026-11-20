"""Integration tests for scope-resolved targeting (pure logic cases)."""

import pytest

from apm_cli.integration.targets import KNOWN_TARGETS

pytestmark = pytest.mark.unit


# -- Copilot scope resolution ------------------------------------------------


# -- OpenCode scope resolution -----------------------------------------------


# -- Codex user-scope behavior ----------------------------------------------


class TestCodexUserScope:
    """Verify Codex participates in user-scope target resolution."""

    def test_for_scope_returns_profile(self):
        codex = KNOWN_TARGETS["codex"]
        assert codex.user_supported == "partial"
        resolved = codex.for_scope(user_scope=True)
        assert resolved is not None
        assert resolved.root_dir == ".codex"
        assert "agents" in resolved.primitives
        assert "skills" in resolved.primitives
        assert "hooks" in resolved.primitives


# -- Claude same-root behavior -----------------------------------------------


class TestClaudeScopeResolution:
    """Verify Claude's scope resolution, including the CLAUDE_CONFIG_DIR
    override at user scope."""

    def test_project_and_user_scope_same_root(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        claude = KNOWN_TARGETS["claude"]
        project = claude.for_scope(user_scope=False)
        user = claude.for_scope(user_scope=True)
        assert project.root_dir == ".claude"
        assert user.root_dir == ".claude"

    def test_all_primitives_available_at_user_scope(self):
        claude = KNOWN_TARGETS["claude"]
        resolved = claude.for_scope(user_scope=True)
        # Claude supports all primitives at user scope
        assert "instructions" in resolved.primitives
        assert "agents" in resolved.primitives

    def test_user_scope_blank_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "   ")
        scoped = KNOWN_TARGETS["claude"].for_scope(user_scope=True)
        assert scoped is not None
        assert scoped.root_dir == ".claude"


# -- resolve_targets consistency ----------------------------------------------


# -- Windsurf scope resolution ------------------------------------------------


# -- Skill deploy at user scope ----------------------------------------------


# -- auto_create guard -------------------------------------------------------
