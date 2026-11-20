"""Unit tests for apm_cli.integration.copilot_cowork_paths (pure logic cases)."""

from __future__ import annotations

import os  # noqa: F401
from pathlib import Path
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest

from apm_cli.integration.copilot_cowork_paths import (
    COWORK_LOCKFILE_PREFIX,  # noqa: F401
    COWORK_URI_SCHEME,  # noqa: F401
    CoworkResolutionError,
    is_cowork_path,
    resolve_copilot_cowork_skills_dir,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# TestResolveCoworkSkillsDir
# ---------------------------------------------------------------------------


class TestResolveCoworkSkillsDir:
    """Tests for resolve_copilot_cowork_skills_dir auto-detection and env override."""

    def test_env_override_traversal_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", "../escape")
        with pytest.raises(CoworkResolutionError, match="traversal"):
            resolve_copilot_cowork_skills_dir()

    def test_env_override_embedded_traversal_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", "/valid/../invalid")
        with pytest.raises(CoworkResolutionError, match="traversal"):
            resolve_copilot_cowork_skills_dir()

    def test_windows_env_var_returns_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", "/tmp/fake-onedrive/skills")
        result = resolve_copilot_cowork_skills_dir()
        assert isinstance(result, Path)

    # -----------------------------------------------------------------------
    # Resolution precedence: config layer
    # -----------------------------------------------------------------------

    def test_config_path_traversal_raises_cowork_resolution_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A traversal sequence in the config value raises CoworkResolutionError."""
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR", raising=False)
        with (
            patch(
                "apm_cli.config.get_copilot_cowork_skills_dir",
                return_value="/valid/../invalid",
            ),
            pytest.raises(CoworkResolutionError, match="traversal"),
        ):
            resolve_copilot_cowork_skills_dir()

    # -----------------------------------------------------------------------
    # Windows auto-detection
    # -----------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestToLockfilePath
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestFromLockfilePath
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestIsCoworkPath
# ---------------------------------------------------------------------------


class TestIsCoworkPath:
    """Tests for is_cowork_path predicate."""

    def test_cowork_uri_returns_true(self) -> None:
        assert is_cowork_path("cowork://skills/foo/SKILL.md") is True

    def test_relative_path_returns_false(self) -> None:
        assert is_cowork_path("relative/path.md") is False

    def test_empty_string_returns_false(self) -> None:
        assert is_cowork_path("") is False
