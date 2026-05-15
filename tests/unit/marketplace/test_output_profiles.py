"""Tests for marketplace output profiles (phase-3a, T-3a-01..08).

Covers:
- path_env_var field presence and shape validation
- _validate_profile rejection of reserved names / bad chars / bad env-vars
- Registration invariants (no duplicates, all pass validation)
- known_output_names() returns correct frozenset
"""

from __future__ import annotations

import pytest

from apm_cli.marketplace.output_profiles import (
    CODEX_MARKETPLACE_OUTPUT,
    DEFAULT_MARKETPLACE_OUTPUT,
    MARKETPLACE_OUTPUTS,
    MarketplaceOutputProfile,
    _validate_profile,
    known_output_names,
)


class TestProfileFields:
    """T-3a-01: Verify all registered profiles have path_env_var."""

    def test_claude_has_path_env_var(self) -> None:
        assert DEFAULT_MARKETPLACE_OUTPUT.path_env_var == "APM_MARKETPLACE_CLAUDE_PATH"

    def test_codex_has_path_env_var(self) -> None:
        assert CODEX_MARKETPLACE_OUTPUT.path_env_var == "APM_MARKETPLACE_CODEX_PATH"

    def test_all_profiles_env_var_pattern(self) -> None:
        import re

        pattern = re.compile(r"^APM_MARKETPLACE_[A-Z0-9_]+_PATH$")
        for profile in MARKETPLACE_OUTPUTS.values():
            assert pattern.fullmatch(profile.path_env_var), (
                f"Profile {profile.name!r} has invalid path_env_var: {profile.path_env_var!r}"
            )


class TestValidateProfile:
    """T-3a-02..05: _validate_profile guards."""

    def test_reserved_name_all(self) -> None:
        p = MarketplaceOutputProfile(
            name="all",
            config_attr="x",
            default_output="x",
            mapper="x",
            path_env_var="APM_MARKETPLACE_ALL_PATH",
        )
        with pytest.raises(ValueError, match="reserved"):
            _validate_profile(p)

    def test_reserved_name_none(self) -> None:
        p = MarketplaceOutputProfile(
            name="none",
            config_attr="x",
            default_output="x",
            mapper="x",
            path_env_var="APM_MARKETPLACE_NONE_PATH",
        )
        with pytest.raises(ValueError, match="reserved"):
            _validate_profile(p)

    def test_invalid_name_with_equals(self) -> None:
        p = MarketplaceOutputProfile(
            name="cla=ude",
            config_attr="x",
            default_output="x",
            mapper="x",
            path_env_var="APM_MARKETPLACE_CLAUDE_PATH",
        )
        with pytest.raises(ValueError, match="CLI-reserved"):
            _validate_profile(p)

    def test_invalid_name_leading_dash(self) -> None:
        p = MarketplaceOutputProfile(
            name="-claude",
            config_attr="x",
            default_output="x",
            mapper="x",
            path_env_var="APM_MARKETPLACE_CLAUDE_PATH",
        )
        with pytest.raises(ValueError, match="CLI-reserved"):
            _validate_profile(p)

    def test_invalid_name_with_comma(self) -> None:
        p = MarketplaceOutputProfile(
            name="cla,ude",
            config_attr="x",
            default_output="x",
            mapper="x",
            path_env_var="APM_MARKETPLACE_CLAUDE_PATH",
        )
        with pytest.raises(ValueError, match="CLI-reserved"):
            _validate_profile(p)

    def test_invalid_env_var_pattern(self) -> None:
        p = MarketplaceOutputProfile(
            name="myformat",
            config_attr="x",
            default_output="x",
            mapper="x",
            path_env_var="MY_CUSTOM_PATH",
        )
        with pytest.raises(ValueError, match="APM_MARKETPLACE_"):
            _validate_profile(p)

    def test_valid_profile_passes(self) -> None:
        p = MarketplaceOutputProfile(
            name="myformat",
            config_attr="myformat",
            default_output="output.json",
            mapper="myformat",
            path_env_var="APM_MARKETPLACE_MYFORMAT_PATH",
        )
        # Should not raise
        _validate_profile(p)


class TestKnownOutputNames:
    """T-3a-06: known_output_names returns frozenset of registered names."""

    def test_returns_frozenset(self) -> None:
        result = known_output_names()
        assert isinstance(result, frozenset)

    def test_contains_claude_and_codex(self) -> None:
        result = known_output_names()
        assert "claude" in result
        assert "codex" in result

    def test_matches_registry_keys(self) -> None:
        assert known_output_names() == frozenset(MARKETPLACE_OUTPUTS.keys())


class TestRegistryInvariants:
    """T-3a-07: Registry-level invariants."""

    def test_no_duplicate_names(self) -> None:
        names = [p.name for p in MARKETPLACE_OUTPUTS.values()]
        assert len(names) == len(set(names))

    def test_no_duplicate_env_vars(self) -> None:
        env_vars = [p.path_env_var for p in MARKETPLACE_OUTPUTS.values()]
        assert len(env_vars) == len(set(env_vars))

    def test_all_registered_profiles_valid(self) -> None:
        """All profiles in the registry pass validation (module-load guard)."""
        for profile in MARKETPLACE_OUTPUTS.values():
            _validate_profile(profile)  # Should not raise
