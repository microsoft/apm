"""Unit tests for apm_cli.version module (pure logic cases)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import apm_cli.version as version_mod
from apm_cli.version import get_build_sha, get_version

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


class TestGetVersionImportlibMetadata:
    """get_version() uses importlib.metadata when not frozen and no constant."""

    def test_importlib_metadata_success(self) -> None:
        # version is locally imported inside get_version(), so patch its source
        with patch.object(version_mod, "__BUILD_VERSION__", None):
            with patch("sys.frozen", False, create=True):
                with patch("importlib.metadata.version", return_value="3.1.4"):
                    assert get_version() == "3.1.4"

    def test_importlib_metadata_legacy_pre38_path(self) -> None:
        """Line 34: Python < 3.8 falls back to importlib_metadata backport."""
        import types

        fake_pkg_not_found = type("PackageNotFoundError", (Exception,), {})
        fake_importlib_metadata = types.ModuleType("importlib_metadata")
        fake_importlib_metadata.PackageNotFoundError = fake_pkg_not_found
        fake_importlib_metadata.version = MagicMock(return_value="7.8.9")

        mock_sys = MagicMock()
        mock_sys.frozen = False
        mock_sys.version_info = (3, 7, 0)  # triggers the else-branch

        with patch.object(version_mod, "__BUILD_VERSION__", None):
            with patch("apm_cli.version.sys", mock_sys):
                with patch.dict("sys.modules", {"importlib_metadata": fake_importlib_metadata}):
                    result = get_version()
        assert result == "7.8.9"


# ---------------------------------------------------------------------------
# get_build_sha()
# ---------------------------------------------------------------------------


class TestGetBuildSha:
    """Tests for get_build_sha()."""

    @pytest.mark.parametrize("frozen", [False, True])
    def test_returns_build_sha_constant_when_set(self, frozen: bool) -> None:
        with (
            patch.object(version_mod, "__BUILD_SHA__", "abc1234"),
            patch("sys.frozen", frozen, create=True),
            patch("subprocess.run") as run,
        ):
            assert get_build_sha() == "abc1234"
        run.assert_not_called()

    @pytest.mark.parametrize("output", ["", "abc1234\n", "/somewhere\n", "/a\n/b\nabc1234\n"])
    def test_unavailable_provenance_omits_sha(self, output: str) -> None:
        with (
            patch.object(version_mod, "__BUILD_SHA__", None),
            patch("sys.frozen", False, create=True),
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=output)),
        ):
            assert get_build_sha() == ""

    def test_build_sha_constant_skips_git(self) -> None:
        with patch.object(version_mod, "__BUILD_SHA__", "deadbeef"):
            with patch("subprocess.run") as mock_run:
                result = get_build_sha()
                mock_run.assert_not_called()
        assert result == "deadbeef"

    def test_git_failure_returns_empty_string(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stdout = ""
        with patch.object(version_mod, "__BUILD_SHA__", None):
            with patch("sys.frozen", False, create=True):
                with patch("subprocess.run", return_value=mock_result):
                    result = get_build_sha()
        assert result == ""

    def test_frozen_mode_returns_empty_string(self) -> None:
        mock_sys = MagicMock()
        mock_sys.frozen = True
        mock_sys._MEIPASS = "/fake/meipass"
        mock_sys.version_info = sys.version_info
        with patch.object(version_mod, "__BUILD_SHA__", None):
            with patch("apm_cli.version.sys", mock_sys):
                with patch("subprocess.run") as mock_run:
                    result = get_build_sha()
                    mock_run.assert_not_called()
        assert result == ""

    def test_git_called_with_correct_args(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abcdef0\n"
        with patch.object(version_mod, "__BUILD_SHA__", None):
            with patch("sys.frozen", False, create=True):
                with patch("subprocess.run", return_value=mock_result) as mock_run:
                    get_build_sha()
        call_args = mock_run.call_args
        assert Path(call_args[0][0][0]).stem == "git"
        assert call_args[0][0][1:] == ["rev-parse", "--show-toplevel", "--short", "HEAD"]
        assert call_args[1].get("timeout") == 5
