"""Unit tests for apm_cli.integration.copilot_cowork_paths (pure logic cases)."""

from __future__ import annotations

import os  # noqa: F401
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest

from apm_cli.integration.copilot_cowork_paths import (
    COWORK_LOCKFILE_PREFIX,  # noqa: F401
    COWORK_URI_SCHEME,  # noqa: F401
    is_cowork_path,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# TestResolveCoworkSkillsDir
# ---------------------------------------------------------------------------


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
