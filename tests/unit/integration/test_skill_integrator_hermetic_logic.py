"""Unit tests for apm_cli.integration.skill_integrator (pure logic cases)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.integration.skill_integrator import (
    should_compile_instructions,
    validate_skill_name,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_package_info(
    install_path: Path,
    package_type=None,
    dep_ref=None,
) -> MagicMock:
    from apm_cli.models.apm_package import PackageType

    pi = MagicMock()
    pi.install_path = install_path
    pi.package_type = package_type or PackageType.CLAUDE_SKILL
    pi.dependency_ref = dep_ref
    return pi


# ---------------------------------------------------------------------------
# validate_skill_name -- line 129 fallthrough
# ---------------------------------------------------------------------------


class TestValidateSkillNameFallthrough:
    """Cover the fallthrough return at line 129."""

    def test_single_hyphen_only(self) -> None:
        """A single hyphen hits the invalid-chars check first."""
        is_valid, _ = validate_skill_name("-")
        assert not is_valid
        # Either leading-hyphen or the fallthrough error
        assert not is_valid

    def test_digit_only_is_valid(self) -> None:
        """Single digit is a valid skill name."""
        is_valid, _ = validate_skill_name("3")
        assert is_valid

    def test_mixed_case_triggers_uppercase_error(self) -> None:
        """Uppercase letter returns specific error before fallthrough."""
        is_valid, msg = validate_skill_name("MySkill")
        assert not is_valid
        assert "lowercase" in msg

    def test_underscore_triggers_underscore_error(self) -> None:
        """Underscore returns specific error before fallthrough."""
        is_valid, msg = validate_skill_name("my_skill")
        assert not is_valid
        assert "underscore" in msg.lower()

    def test_space_triggers_space_error(self) -> None:
        """Space returns specific error."""
        is_valid, msg = validate_skill_name("my skill")
        assert not is_valid
        assert "space" in msg.lower()

    def test_special_char_triggers_invalid_chars_error(self) -> None:
        """Special character returns 'invalid characters' error."""
        is_valid, msg = validate_skill_name("my@skill")
        assert not is_valid
        assert "invalid character" in msg.lower()


# ---------------------------------------------------------------------------
# should_compile_instructions
# ---------------------------------------------------------------------------


class TestShouldCompileInstructions:
    """Cover lines 258, 260, 264."""

    def test_instructions_type_compiles(self) -> None:
        from apm_cli.models.apm_package import PackageContentType, PackageType

        pi = _make_package_info(Path("/x/y"), package_type=PackageType.APM_PACKAGE)
        with patch(
            "apm_cli.integration.skill_integrator.get_effective_type",
            return_value=PackageContentType.INSTRUCTIONS,
        ):
            assert should_compile_instructions(pi) is True

    def test_hybrid_type_compiles(self) -> None:
        from apm_cli.models.apm_package import PackageContentType, PackageType

        pi = _make_package_info(Path("/x/y"), package_type=PackageType.HYBRID)
        with patch(
            "apm_cli.integration.skill_integrator.get_effective_type",
            return_value=PackageContentType.HYBRID,
        ):
            assert should_compile_instructions(pi) is True

    def test_skill_type_does_not_compile(self) -> None:
        from apm_cli.models.apm_package import PackageContentType, PackageType

        pi = _make_package_info(Path("/x/y"), package_type=PackageType.CLAUDE_SKILL)
        with patch(
            "apm_cli.integration.skill_integrator.get_effective_type",
            return_value=PackageContentType.SKILL,
        ):
            assert should_compile_instructions(pi) is False

    def test_prompts_type_does_not_compile(self) -> None:
        from apm_cli.models.apm_package import PackageContentType, PackageType

        pi = _make_package_info(Path("/x/y"), package_type=PackageType.APM_PACKAGE)
        with patch(
            "apm_cli.integration.skill_integrator.get_effective_type",
            return_value=PackageContentType.PROMPTS,
        ):
            assert should_compile_instructions(pi) is False


# ---------------------------------------------------------------------------
# copy_skill_to_target -- should_install_skill=False (line 305)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _dircmp_equal -- lines 519, 526-527
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _promote_sub_skills -- non-dir entry is skipped (line 578)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _promote_sub_skills -- managed_files unmanaged + logger branch (lines 608-609)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _promote_sub_skills_standalone -- dedup via seen_skill_dirs (lines 761-762)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _build_skill_ownership_map / _build_native_skill_owner_map
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _integrate_native_skill -- name normalization with logger (lines 844-856)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _integrate_native_skill -- collision warning with logger (line 958-959)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# integrate -- virtual file skip (line 1188)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# integrate -- skill_subset warning for native SKILL.md (lines 1203, 1205)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# integrate -- skill bundle path (lines 1221-1225)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# integrate -- sub-skills standalone (lines 1239, 1248)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _integrate_skill_bundle -- dedup second target (lines 1084-1090)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Executable gate denial path -- skip_bin blocks bin/ during promotion
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _integrate_skill_bundle -- --skill filter matches nothing (issue #2116)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _promote_sub_skills_standalone -- --skill filter matches nothing (#2116)
# ---------------------------------------------------------------------------
