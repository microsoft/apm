"""Tests for get_build_sha() function."""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import apm_cli.version as version_mod
from apm_cli.version import get_build_sha


class TestGetBuildSha(unittest.TestCase):
    """Test build SHA retrieval across all code paths."""

    @patch("apm_cli.version.__BUILD_SHA__", "abc1234")
    def test_returns_build_time_constant_when_set(self):
        """Build-time constant takes priority over git."""
        assert get_build_sha() == "abc1234"

    @patch("apm_cli.version.__BUILD_SHA__", None)
    def test_returns_empty_when_frozen_and_no_constant(self):
        """Frozen binary without build constant returns empty string."""
        with patch.object(__import__("sys"), "frozen", True, create=True):
            assert get_build_sha() == ""

    @patch("apm_cli.version.__BUILD_SHA__", None)
    @patch("subprocess.run")
    def test_falls_back_to_git_in_development(self, mock_run):
        """In development, verifies the checkout root and tracked source."""
        root = Path(version_mod.__file__).resolve().parents[2]
        mock_run.return_value = MagicMock(returncode=0, stdout=f"{root}\nd1630d1\n")
        with patch("sys.frozen", False, create=True):
            result = get_build_sha()
        assert result == "d1630d1"
        assert mock_run.call_count == 2
        args = mock_run.call_args_list[0]
        assert Path(args[0][0][0]).stem == "git"
        assert args[0][0][1:] == ["rev-parse", "--show-toplevel", "--short", "HEAD"]
        assert mock_run.call_args_list[1].args[0][1:] == [
            "ls-files",
            "--error-unmatch",
            "--",
            "src/apm_cli/version.py",
            "pyproject.toml",
        ]

    @patch("apm_cli.version.__BUILD_SHA__", None)
    @patch("subprocess.run", side_effect=FileNotFoundError("git not found"))
    def test_returns_empty_when_git_unavailable(self, _mock_run):
        """Returns empty string when git is not installed."""
        with patch("sys.frozen", False, create=True):
            assert get_build_sha() == ""

    @patch("apm_cli.version.__BUILD_SHA__", None)
    @patch("subprocess.run")
    def test_returns_empty_when_git_fails(self, mock_run):
        """Returns empty string when git command fails (e.g., not a repo)."""
        mock_run.return_value = MagicMock(returncode=128, stdout="")
        with patch("sys.frozen", False, create=True):
            assert get_build_sha() == ""

    @patch("apm_cli.version.__BUILD_SHA__", None)
    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5))
    def test_returns_empty_on_timeout(self, _mock_run):
        """Returns empty string when git times out."""
        with patch("sys.frozen", False, create=True):
            assert get_build_sha() == ""
