"""Unit tests for apm_cli.version module (pure logic cases)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import apm_cli.version as version_mod
from apm_cli.version import get_version

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# get_version()
# ---------------------------------------------------------------------------


class TestGetVersionBuildConstant:
    """get_version() returns __BUILD_VERSION__ when it is set."""

    def test_returns_build_version_constant(self) -> None:
        with patch.object(version_mod, "__BUILD_VERSION__", "1.2.3"):
            assert get_version() == "1.2.3"

    def test_build_version_takes_priority_over_importlib(self) -> None:
        """Build constant must be checked before importlib.metadata."""
        with patch.object(version_mod, "__BUILD_VERSION__", "9.9.9"):
            # even if importlib would succeed, we never reach it
            with patch("importlib.metadata.version", return_value="0.0.1") as mock_meta:
                assert get_version() == "9.9.9"
                mock_meta.assert_not_called()


# ---------------------------------------------------------------------------
# get_build_sha()
# ---------------------------------------------------------------------------
