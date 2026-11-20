"""Copilot user-scope target profile."""

from __future__ import annotations

import pytest

from apm_cli.integration.targets import KNOWN_TARGETS

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Target profile: user-scope copilot
# ---------------------------------------------------------------------------


class TestCopilotUserScopeTargetProfile:
    """Copilot target must support instructions at user scope."""

    def test_instructions_supported_at_user_scope(self):
        """instructions must NOT be listed in unsupported_user_primitives."""
        copilot = KNOWN_TARGETS["copilot"]
        assert "instructions" not in copilot.unsupported_user_primitives

    def test_supports_at_user_scope_instructions_true(self):
        """supports_at_user_scope('instructions') must be True for copilot."""
        copilot = KNOWN_TARGETS["copilot"]
        assert copilot.supports_at_user_scope("instructions") is True

    def test_for_scope_user_instructions_uses_modular_format(self):
        """for_scope(user_scope=True) must keep the standard github_instructions mapping."""
        copilot = KNOWN_TARGETS["copilot"]
        user_profile = copilot.for_scope(user_scope=True)
        assert user_profile is not None
        mapping = user_profile.primitives.get("instructions")
        assert mapping is not None
        assert mapping.format_id == "github_instructions"
        assert mapping.subdir == "instructions"

    def test_for_scope_user_root_is_copilot_dir(self):
        """User-scope profile must have root_dir == '.copilot'."""
        copilot = KNOWN_TARGETS["copilot"]
        user_profile = copilot.for_scope(user_scope=True)
        assert user_profile is not None
        assert user_profile.root_dir == ".copilot"

    def test_for_scope_user_generated_files_empty(self):
        """User-scope profile must not carry the project-scope compile output.

        ``copilot-instructions.md`` is a compile-time output against the fixed
        project root; it has no user-scope equivalent and must not leak into
        user-scope lockfile/drift tracking.
        """
        copilot = KNOWN_TARGETS["copilot"]
        user_profile = copilot.for_scope(user_scope=True)
        assert user_profile is not None
        assert user_profile.generated_files == ()


# ---------------------------------------------------------------------------
# Integration: instruction files -> ~/.copilot/instructions/*.instructions.md
# ---------------------------------------------------------------------------
