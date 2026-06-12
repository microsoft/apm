"""Tests for ``apm cache`` commands — coverage for missed lines.

Missed lines:
- 21-25: info() → get_cache_root raises
- 67-71: clean() → get_cache_root raises
- 75->79: clean() confirm=no → aborts
- 114-118: prune() → get_cache_root raises
- 132-137: _format_size for MB and GB ranges
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


class TestCacheInfo:
    def test_info_success(self, runner) -> None:
        """Happy path: cache info shows size statistics."""
        mock_git_stats = {"total_size_bytes": 1024, "db_count": 2, "checkout_count": 3}
        mock_http_stats = {"total_size_bytes": 2048, "entry_count": 5}

        with (
            patch("apm_cli.cache.paths.get_cache_root", return_value="/tmp/cache"),
            patch("apm_cli.cache.git_cache.GitCache") as MockGit,
            patch("apm_cli.cache.http_cache.HttpCache") as MockHttp,
        ):
            MockGit.return_value.get_cache_stats.return_value = mock_git_stats
            MockHttp.return_value.get_stats.return_value = mock_http_stats
            result = runner.invoke(cli, ["cache", "info"])

        assert result.exit_code == 0, result.output

    def test_info_cache_root_error(self, runner) -> None:
        """Lines 21-25: get_cache_root raises ValueError → error + exit 1."""
        with patch(
            "apm_cli.cache.paths.get_cache_root",
            side_effect=ValueError("no home dir"),
        ):
            result = runner.invoke(cli, ["cache", "info"])

        assert result.exit_code == 1


class TestCacheClean:
    def test_clean_force_skips_confirmation(self, runner) -> None:
        """--force flag skips confirm and cleans."""
        with (
            patch("apm_cli.cache.paths.get_cache_root", return_value="/tmp/cache"),
            patch("apm_cli.cache.git_cache.GitCache") as MockGit,
            patch("apm_cli.cache.http_cache.HttpCache") as MockHttp,
        ):
            MockGit.return_value.clean_all.return_value = None
            MockHttp.return_value.clean_all.return_value = None
            result = runner.invoke(cli, ["cache", "clean", "--force"])

        assert result.exit_code == 0, result.output

    def test_clean_cache_root_error(self, runner) -> None:
        """Lines 67-71: get_cache_root raises OSError → error + exit 1."""
        with patch(
            "apm_cli.cache.paths.get_cache_root",
            side_effect=OSError("permission denied"),
        ):
            result = runner.invoke(cli, ["cache", "clean", "--force"])

        assert result.exit_code == 1

    def test_clean_confirm_no_aborts(self, runner) -> None:
        """Lines 75->79: user declines confirm → aborted, no cleaning."""
        with (
            patch("apm_cli.cache.paths.get_cache_root", return_value="/tmp/cache"),
        ):
            # CliRunner input="n\n" simulates user typing 'n' at the confirm prompt
            result = runner.invoke(cli, ["cache", "clean"], input="n\n")

        assert result.exit_code == 0, result.output

    def test_clean_yes_flag_skips_confirmation(self, runner) -> None:
        """--yes flag skips confirm and cleans."""
        with (
            patch("apm_cli.cache.paths.get_cache_root", return_value="/tmp/cache"),
            patch("apm_cli.cache.git_cache.GitCache") as MockGit,
            patch("apm_cli.cache.http_cache.HttpCache") as MockHttp,
        ):
            MockGit.return_value.clean_all.return_value = None
            MockHttp.return_value.clean_all.return_value = None
            result = runner.invoke(cli, ["cache", "clean", "--yes"])

        assert result.exit_code == 0, result.output


class TestCachePrune:
    def test_prune_success(self, runner) -> None:
        """Happy path: prune removes stale entries."""
        with (
            patch("apm_cli.cache.paths.get_cache_root", return_value="/tmp/cache"),
            patch("apm_cli.cache.git_cache.GitCache") as MockGit,
        ):
            MockGit.return_value.prune.return_value = 3
            result = runner.invoke(cli, ["cache", "prune"])

        assert result.exit_code == 0, result.output
        assert "3" in result.output

    def test_prune_cache_root_error(self, runner) -> None:
        """Lines 114-118: get_cache_root raises → error + exit 1."""
        with patch(
            "apm_cli.cache.paths.get_cache_root",
            side_effect=ValueError("cache unavailable"),
        ):
            result = runner.invoke(cli, ["cache", "prune"])

        assert result.exit_code == 1


class TestFormatSize:
    """Lines 132-137: _format_size for all size ranges."""

    def test_bytes_range(self) -> None:
        from apm_cli.commands.cache import _format_size

        assert _format_size(512) == "512 B"

    def test_kilobytes_range(self) -> None:
        from apm_cli.commands.cache import _format_size

        result = _format_size(2048)
        assert "KB" in result

    def test_megabytes_range(self) -> None:
        """Line 133-134: MB branch."""
        from apm_cli.commands.cache import _format_size

        result = _format_size(5 * 1024 * 1024)
        assert "MB" in result

    def test_gigabytes_range(self) -> None:
        """Lines 135-137: GB branch."""
        from apm_cli.commands.cache import _format_size

        result = _format_size(2 * 1024 * 1024 * 1024)
        assert "GB" in result
