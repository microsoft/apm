"""Unit tests for apm_cli.core.experimental (feature-flag subsystem).

Coverage:
  - is_enabled: default, config override, unknown flag
  - enable / disable / reset: round-trips on an isolated disk config
  - normalise_flag_name: hyphen and underscore inputs
  - validate_flag_name: ValueError raised before any write, difflib suggestions
  - Loader rejection of non-bool values in config
  - get_overridden_flags / get_stale_config_keys / get_malformed_flag_keys
  - Registry invariants (key == name, all defaults False, printable ASCII)
"""

from __future__ import annotations

import re
from typing import Any, Dict  # noqa: F401, UP035
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Reset the in-process config cache before and after every test."""
    from apm_cli.config import _invalidate_config_cache

    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


@pytest.fixture
def inject_config(monkeypatch):
    """Directly inject a dict into the config cache -- no disk I/O."""
    import apm_cli.config as _conf

    def _set(cfg: dict[str, Any]) -> None:
        monkeypatch.setattr(_conf, "_config_cache", cfg)

    return _set


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


class TestIsEnabled:
    """Tests for the core is_enabled query."""

    def test_returns_false_when_no_override(self, inject_config: Any) -> None:
        """Registry default (False) is returned when config has no experimental section."""
        inject_config({})
        from apm_cli.core.experimental import is_enabled

        assert is_enabled("verbose_version") is False

    def test_returns_true_from_config_override(self, inject_config: Any) -> None:
        """Returns True when config.json sets verbose_version to True."""
        inject_config({"experimental": {"verbose_version": True}})
        from apm_cli.core.experimental import is_enabled

        assert is_enabled("verbose_version") is True

    def test_unknown_flag_raises_value_error(self, inject_config: Any) -> None:
        """is_enabled raises ValueError for an unregistered flag name."""
        inject_config({})
        from apm_cli.core.experimental import is_enabled

        with pytest.raises(ValueError, match="Unknown experimental flag"):
            is_enabled("totally_unknown_flag_xyz")


# ---------------------------------------------------------------------------
# Mutators: enable / disable / reset  (round-trips via isolated disk config)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _normalise_flag_name
# ---------------------------------------------------------------------------


class TestNormaliseFlagName:
    """Tests for the name-normalisation helper."""

    @pytest.mark.parametrize(
        "raw",
        ["verbose-version", "verbose_version"],
        ids=["kebab-to-snake", "snake-idempotent"],
    )
    def test_normalises_to_snake_case(self, raw) -> None:
        from apm_cli.core.experimental import normalise_flag_name

        assert normalise_flag_name(raw) == "verbose_version"


# ---------------------------------------------------------------------------
# _validate_flag_name: validation, ValueError, difflib suggestions
# ---------------------------------------------------------------------------


class TestValidateFlagName:
    """Tests for validate_flag_name -- the public validation entry point."""

    def test_unknown_flag_raises_value_error_before_config_write(
        self, inject_config: Any, monkeypatch: Any
    ) -> None:
        """ValueError is raised for an unknown flag and update_config is never called.

        This verifies the meaningful contract: that validation rejects the
        name *before* any write to the config file can occur.  We call
        ``enable()`` (which would write) with an unknown name and assert
        that ``update_config`` was never reached.
        """
        inject_config({})
        mock_update = MagicMock()
        monkeypatch.setattr("apm_cli.config.update_config", mock_update)

        from apm_cli.core.experimental import enable, validate_flag_name

        # validate_flag_name itself should raise
        with pytest.raises(ValueError, match="Unknown experimental feature"):
            validate_flag_name("nonexistent-flag-abc123")

        # enable() also raises for unregistered names (KeyError from FLAGS)
        with pytest.raises(KeyError):
            enable("nonexistent_flag_abc123")

        assert mock_update.call_count == 0

    def test_value_error_args_contain_difflib_suggestion(self) -> None:
        """For a near-typo, exc.args[1] contains difflib suggestion list."""
        from apm_cli.core.experimental import validate_flag_name

        with pytest.raises(ValueError) as exc_info:
            validate_flag_name("verbse-version")  # one char typo

        exc = exc_info.value
        assert "Unknown experimental feature" in exc.args[0]
        suggestions = exc.args[1]
        assert isinstance(suggestions, list), "suggestions should be a list"
        assert "verbose-version" in suggestions

    def test_value_error_no_suggestion_for_distant_name(self) -> None:
        """When the flag name is far from all known flags, suggestions list is empty."""
        from apm_cli.core.experimental import validate_flag_name

        with pytest.raises(ValueError) as exc_info:
            validate_flag_name("zzzz-completely-unrelated-xyzqwerty")

        suggestions = exc_info.value.args[1]
        assert suggestions == []

    def test_valid_flag_returns_normalised_name(self) -> None:
        """Known flag names (hyphen or underscore) return the snake_case form."""
        from apm_cli.core.experimental import validate_flag_name

        assert validate_flag_name("verbose-version") == "verbose_version"
        assert validate_flag_name("verbose_version") == "verbose_version"


# ---------------------------------------------------------------------------
# Loader rejection of non-bool config values
# ---------------------------------------------------------------------------


class TestLoaderRejectsNonBool:
    """Non-bool values in config are rejected; the registry default is returned."""

    @pytest.mark.parametrize(
        "bad_value",
        ["yes", "true", "false", 1, 0, 1.0, 0.0, [], {}, None],
        ids=[
            "yes",
            "true_str",
            "false_str",
            "int_1",
            "int_0",
            "float_1",
            "float_0",
            "list",
            "dict",
            "none",
        ],
    )
    def test_non_bool_falls_back_to_registry_default(
        self, inject_config: Any, bad_value: Any
    ) -> None:
        """Any non-bool config value causes fallback to registry default (False)."""
        inject_config({"experimental": {"verbose_version": bad_value}})
        from apm_cli.core.experimental import is_enabled

        assert is_enabled("verbose_version") is False


# ---------------------------------------------------------------------------
# get_overridden_flags
# ---------------------------------------------------------------------------


class TestGetOverriddenFlags:
    """Tests for the get_overridden_flags helper."""

    def test_returns_only_registered_known_flags(self, inject_config: Any) -> None:
        """Stale keys (not in FLAGS) are excluded; known bool flags are included."""
        inject_config(
            {
                "experimental": {
                    "verbose_version": True,
                    "stale_removed_flag_xyz": True,  # unknown key
                }
            }
        )
        from apm_cli.core.experimental import get_overridden_flags

        result = get_overridden_flags()
        assert "verbose_version" in result
        assert result["verbose_version"] is True
        assert "stale_removed_flag_xyz" not in result

    def test_excludes_non_bool_values(self, inject_config: Any) -> None:
        """Non-bool config values are excluded from the override map."""
        inject_config({"experimental": {"verbose_version": "yes"}})
        from apm_cli.core.experimental import get_overridden_flags

        assert get_overridden_flags() == {}

    def test_empty_when_no_experimental_section(self, inject_config: Any) -> None:
        """Empty dict is returned when config has no experimental section."""
        inject_config({})
        from apm_cli.core.experimental import get_overridden_flags

        assert get_overridden_flags() == {}


# ---------------------------------------------------------------------------
# get_stale_config_keys
# ---------------------------------------------------------------------------


class TestGetStaleConfigKeys:
    """Tests for the stale-key detection helper."""

    def test_returns_keys_not_in_flags(self, inject_config: Any) -> None:
        """Keys present in config but absent from FLAGS are reported as stale."""
        inject_config(
            {
                "experimental": {
                    "verbose_version": True,  # known
                    "old_deprecated_flag_abc": True,  # stale
                }
            }
        )
        from apm_cli.core.experimental import get_stale_config_keys

        result = get_stale_config_keys()
        assert "old_deprecated_flag_abc" in result
        assert "verbose_version" not in result

    def test_empty_when_all_keys_known(self, inject_config: Any) -> None:
        """No stale keys when experimental section contains only registered flags."""
        inject_config({"experimental": {"verbose_version": True}})
        from apm_cli.core.experimental import get_stale_config_keys

        assert get_stale_config_keys() == []

    def test_empty_when_no_experimental_section(self, inject_config: Any) -> None:
        """Empty list when config has no experimental section at all."""
        inject_config({})
        from apm_cli.core.experimental import get_stale_config_keys

        assert get_stale_config_keys() == []


# ---------------------------------------------------------------------------
# Non-dict experimental config guard (A3)
# ---------------------------------------------------------------------------


class TestNonDictExperimentalConfig:
    """When ``experimental`` is not a dict, all queries fail closed."""

    @pytest.mark.parametrize(
        "bad_value",
        [42, "oops", True, 3.14, []],
        ids=["int", "str", "bool", "float", "list"],
    )
    def test_is_enabled_returns_false_on_non_dict_experimental(
        self, inject_config: Any, bad_value: Any
    ) -> None:
        """is_enabled returns False (registry default) when experimental is non-dict."""
        inject_config({"experimental": bad_value})
        from apm_cli.core.experimental import is_enabled

        assert is_enabled("verbose_version") is False

    @pytest.mark.parametrize(
        "bad_value",
        [42, "oops", True, 3.14, []],
        ids=["int", "str", "bool", "float", "list"],
    )
    def test_get_overridden_flags_returns_empty_on_non_dict(
        self, inject_config: Any, bad_value: Any
    ) -> None:
        """get_overridden_flags returns {} when experimental is non-dict."""
        inject_config({"experimental": bad_value})
        from apm_cli.core.experimental import get_overridden_flags

        assert get_overridden_flags() == {}

    @pytest.mark.parametrize(
        "bad_value",
        [42, "oops", True, 3.14, []],
        ids=["int", "str", "bool", "float", "list"],
    )
    def test_get_stale_config_keys_returns_empty_on_non_dict(
        self, inject_config: Any, bad_value: Any
    ) -> None:
        """get_stale_config_keys returns [] when experimental is non-dict."""
        inject_config({"experimental": bad_value})
        from apm_cli.core.experimental import get_stale_config_keys

        assert get_stale_config_keys() == []


# ---------------------------------------------------------------------------
# Registry invariants
# ---------------------------------------------------------------------------


class TestRegistryInvariants:
    """Static integrity checks for the FLAGS registry."""

    def test_registry_invariants(self) -> None:
        """
        Verify three structural invariants for every registered flag:
          1. FLAGS key matches the flag's .name attribute.
          2. Every flag's .default is False (opt-in only).
          3. description and hint (if present) contain only printable ASCII.
        """
        from apm_cli.core.experimental import FLAGS

        printable_ascii = re.compile(r"[\x20-\x7e]+")

        for key, flag in FLAGS.items():
            # Invariant 1: key == name
            assert key == flag.name, (
                f"Registry key mismatch: key={key!r} but flag.name={flag.name!r}"
            )

            # Invariant 2: all defaults must be False
            assert flag.default is False, (
                f"Flag {flag.name!r} has non-False default: {flag.default!r}"
            )

            # Invariant 3: description must be printable ASCII
            assert re.fullmatch(printable_ascii.pattern, flag.description), (
                f"Flag {flag.name!r} description contains non-printable-ASCII: {flag.description!r}"
            )

            # Invariant 3 (continued): hint, when present, must also be printable ASCII
            if flag.hint is not None:
                assert re.fullmatch(printable_ascii.pattern, flag.hint), (
                    f"Flag {flag.name!r} hint contains non-printable-ASCII: {flag.hint!r}"
                )


# ---------------------------------------------------------------------------
# get_malformed_flag_keys
# ---------------------------------------------------------------------------


class TestGetMalformedFlagKeys:
    """Tests for the malformed-value detection helper."""

    def test_returns_known_flag_with_non_bool_value(self, inject_config: Any) -> None:
        """A registered flag with a string value is reported as malformed."""
        inject_config({"experimental": {"verbose_version": "true"}})
        from apm_cli.core.experimental import get_malformed_flag_keys

        result = get_malformed_flag_keys()
        assert "verbose_version" in result

    def test_excludes_bool_overrides(self, inject_config: Any) -> None:
        """A registered flag with a proper bool value is NOT malformed."""
        inject_config({"experimental": {"verbose_version": True}})
        from apm_cli.core.experimental import get_malformed_flag_keys

        assert get_malformed_flag_keys() == []

    def test_excludes_unknown_keys(self, inject_config: Any) -> None:
        """Unknown keys (stale) are NOT reported as malformed."""
        inject_config({"experimental": {"unknown_flag_xyz": "garbage"}})
        from apm_cli.core.experimental import get_malformed_flag_keys

        assert get_malformed_flag_keys() == []

    def test_empty_when_no_experimental_section(self, inject_config: Any) -> None:
        """Empty list when config has no experimental section at all."""
        inject_config({})
        from apm_cli.core.experimental import get_malformed_flag_keys

        assert get_malformed_flag_keys() == []


# ---------------------------------------------------------------------------
# reset return type
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cowork flag registration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Package registry flag registration
# ---------------------------------------------------------------------------
