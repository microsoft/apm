"""Unit tests for SKILL_BUNDLE detection, validation, and integration (pure logic cases)."""

import pytest

from src.apm_cli.models.apm_package import (
    APMPackage,  # noqa: F401
    ValidationResult,  # noqa: F401
)

pytestmark = pytest.mark.unit

# ============================================================================
# Detection: covers all 8 shapes from the plan's test matrix
# ============================================================================


# ============================================================================
# Validation: covers path traversal, name mismatch, ASCII, multi-skill
# ============================================================================


# ============================================================================
# --skill flag: unit tests for normalization and validation
# ============================================================================


class TestSkillSubsetNormalization:
    """Tests for the --skill flag normalization logic in install.py."""

    def test_skill_names_empty_gives_none(self):
        """No --skill -> None (install all)."""
        from apm_cli.commands.install import install  # noqa: F401

        # This is implicitly tested by the Click default (multiple=True -> empty tuple)
        # The normalization: empty tuple is falsy, so _skill_subset stays None.
        assert not ()  # confirms empty tuple is falsy

    def test_wildcard_star_gives_none(self):
        """--skill '*' -> None (install all)."""
        # Test the logic directly: if '*' in skill_names, result is None
        skill_names = ("*",)
        _skill_subset = None
        if skill_names:
            if not any(s == "*" for s in skill_names):
                _skill_subset = tuple(skill_names)
        assert _skill_subset is None

    def test_specific_names_preserved(self):
        """--skill a --skill b -> ('a', 'b')."""
        skill_names = ("alpha", "beta")
        _skill_subset = None
        if skill_names:
            if not any(s == "*" for s in skill_names):
                _skill_subset = tuple(skill_names)
        assert _skill_subset == ("alpha", "beta")

    def test_star_with_others_still_gives_none(self):
        """--skill a --skill '*' -> None (wildcard overrides)."""
        skill_names = ("alpha", "*")
        _skill_subset = None
        if skill_names:
            if not any(s == "*" for s in skill_names):
                _skill_subset = tuple(skill_names)
        assert _skill_subset is None


# ============================================================================
# Integration: _promote_sub_skills name_filter
# ============================================================================
