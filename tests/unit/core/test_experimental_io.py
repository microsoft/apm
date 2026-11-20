"""Unit tests for apm_cli.core.experimental (feature-flag subsystem) (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import json
from typing import Any, Dict  # noqa: F401, UP035

import pytest

pytestmark = pytest.mark.component

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
def isolated_config(tmp_path, monkeypatch):
    """Redirect CONFIG_FILE to a temp dir so mutator tests never hit ~/.apm.

    Returns the Path to the config.json file for post-write inspection.
    ``ensure_config_exists()`` (called by ``get_config()``) will create the
    directory and file on first access.
    """
    import apm_cli.config as _conf

    config_dir = tmp_path / ".apm"
    config_file = config_dir / "config.json"
    monkeypatch.setattr(_conf, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(config_file))
    return config_file


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Mutators: enable / disable / reset  (round-trips via isolated disk config)
# ---------------------------------------------------------------------------


class TestMutators:
    """Tests for enable, disable, and reset writing through to config.json."""

    def test_enable_roundtrip_is_enabled_returns_true(self, isolated_config: Any) -> None:
        """enable() followed by is_enabled() returns True (no manual reload)."""
        from apm_cli.core.experimental import enable, is_enabled

        enable("verbose_version")
        assert is_enabled("verbose_version") is True

    def test_disable_after_enable_returns_false_and_persists(self, isolated_config: Any) -> None:
        """disable() after enable() sets the flag to False and persists the value."""
        from apm_cli.core.experimental import disable, enable, is_enabled

        enable("verbose_version")
        disable("verbose_version")
        assert is_enabled("verbose_version") is False

        # Also verify the False value was persisted to disk.
        data = json.loads(isolated_config.read_text(encoding="utf-8"))
        assert data.get("experimental", {}).get("verbose_version") is False

    def test_reset_single_flag_removes_key_from_config(self, isolated_config: Any) -> None:
        """reset(name) removes the key from config.json entirely (not just False)."""
        from apm_cli.core.experimental import enable, reset

        enable("verbose_version")
        reset("verbose_version")

        data = json.loads(isolated_config.read_text(encoding="utf-8"))
        assert "verbose_version" not in data.get("experimental", {})

    def test_reset_all_clears_experimental_section(self, isolated_config: Any) -> None:
        """reset() with no args clears the entire experimental dict in config."""
        from apm_cli.core.experimental import enable, reset

        enable("verbose_version")
        reset(None)  # bulk reset

        data = json.loads(isolated_config.read_text(encoding="utf-8"))
        assert data.get("experimental", {}) == {}


# ---------------------------------------------------------------------------
# _normalise_flag_name
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_flag_name: validation, ValueError, difflib suggestions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Loader rejection of non-bool config values
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_overridden_flags
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_stale_config_keys
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Non-dict experimental config guard (A3)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Registry invariants
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_malformed_flag_keys
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# reset return type
# ---------------------------------------------------------------------------


class TestResetReturnType:
    """Tests that reset() returns an int count, not a list."""

    def test_reset_single_returns_int(self, isolated_config: Any) -> None:
        """reset(name) returns 1 when the key existed, 0 otherwise."""
        from apm_cli.core.experimental import enable, reset

        enable("verbose_version")
        result = reset("verbose_version")
        assert result == 1
        assert isinstance(result, int)

    def test_reset_single_noop_returns_zero(self, isolated_config: Any) -> None:
        """reset(name) returns 0 when nothing was in config."""
        from apm_cli.core.experimental import reset

        result = reset("verbose_version")
        assert result == 0
        assert isinstance(result, int)

    def test_reset_bulk_returns_count(self, isolated_config: Any) -> None:
        """reset(None) returns the number of keys that were removed."""
        from apm_cli.core.experimental import enable, reset

        enable("verbose_version")
        result = reset(None)
        assert result == 1
        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Cowork flag registration
# ---------------------------------------------------------------------------


class TestCoworkFlagRegistration:
    """Tests for the 'cowork' experimental flag registration."""

    def test_cowork_flag_can_be_enabled(self, isolated_config: Any) -> None:
        from apm_cli.core.experimental import enable, is_enabled

        enable("copilot_cowork")
        assert is_enabled("copilot_cowork") is True


# ---------------------------------------------------------------------------
# Package registry flag registration
# ---------------------------------------------------------------------------


class TestPackageRegistryFlagRegistration:
    """Tests for the registries experimental flag registration."""

    def test_package_registry_flag_can_be_enabled(self, isolated_config: Any) -> None:
        from apm_cli.core.experimental import enable, is_enabled

        enable("registries")
        assert is_enabled("registries") is True
