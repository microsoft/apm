"""Tests for apm cache CLI commands."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.cache import cache


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestCacheInfo:
    """Test `apm cache info` command."""

    @patch("apm_cli.cache.paths.get_cache_root")
    def test_shows_cache_stats(
        self, mock_root: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        mock_root.return_value = tmp_path
        # Create minimal cache structure
        (tmp_path / "git" / "db_v1").mkdir(parents=True)
        (tmp_path / "git" / "checkouts_v1").mkdir(parents=True)
        (tmp_path / "http_v1").mkdir(parents=True)

        result = runner.invoke(cache, ["info"])
        assert result.exit_code == 0
        assert "Cache root:" in result.output
        assert "Git repositories" in result.output
        assert "HTTP cache entries" in result.output


class TestCacheClean:
    """Test `apm cache clean` command."""

    @patch("apm_cli.cache.paths.get_cache_root")
    def test_clean_with_force(
        self, mock_root: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        mock_root.return_value = tmp_path
        (tmp_path / "git" / "db_v1" / "shard1").mkdir(parents=True)
        (tmp_path / "git" / "checkouts_v1").mkdir(parents=True)
        (tmp_path / "http_v1").mkdir(parents=True)

        result = runner.invoke(cache, ["clean", "--force"])
        assert result.exit_code == 0
        assert "cleaned" in result.output.lower()

    @patch("apm_cli.cache.paths.get_cache_root")
    def test_clean_aborted_without_confirmation(
        self, mock_root: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        mock_root.return_value = tmp_path
        (tmp_path / "git" / "db_v1").mkdir(parents=True)
        (tmp_path / "git" / "checkouts_v1").mkdir(parents=True)
        (tmp_path / "http_v1").mkdir(parents=True)

        result = runner.invoke(cache, ["clean"], input="n\n")
        assert result.exit_code == 0
        assert "aborted" in result.output.lower()


class TestCachePrune:
    """Test `apm cache prune` command."""

    @patch("apm_cli.cache.paths.get_cache_root")
    def test_prune_default_days(
        self, mock_root: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        mock_root.return_value = tmp_path
        (tmp_path / "git" / "db_v1").mkdir(parents=True)
        (tmp_path / "git" / "checkouts_v1").mkdir(parents=True)
        (tmp_path / "http_v1").mkdir(parents=True)

        result = runner.invoke(cache, ["prune"])
        assert result.exit_code == 0
        assert "Pruning SHA groups older than 30 days..." in result.output
        assert "Pruned 0 SHA group(s)." in result.output

    @patch("apm_cli.cache.paths.get_cache_root")
    def test_prune_custom_days(
        self, mock_root: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        mock_root.return_value = tmp_path
        (tmp_path / "git" / "db_v1").mkdir(parents=True)
        (tmp_path / "git" / "checkouts_v1").mkdir(parents=True)
        (tmp_path / "http_v1").mkdir(parents=True)
        stale = tmp_path / "git/checkouts_v1/shard/stale"
        (stale / "full").mkdir(parents=True)
        (stale / "sparse-variant").mkdir()
        os.utime(stale, ns=(946684800000000000, 946684800000000000))

        result = runner.invoke(cache, ["prune", "--days", "7"])
        assert result.exit_code == 0
        assert "Pruning SHA groups older than 7 days..." in result.output
        assert "Pruned 1 SHA group(s)." in result.output
        assert not stale.exists()
