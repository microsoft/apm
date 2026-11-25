"""Tests for version checker utility (filesystem, process and CLI boundary cases)."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.utils.version_checker import (
    save_version_check_timestamp,
    should_check_for_updates,
)

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-release-metadata-discovery")]


class TestVersionCheckCache(unittest.TestCase):
    """Test version check caching functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.cache_file = Path(self.temp_dir) / "last_version_check"

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil

        if Path(self.temp_dir).exists():
            shutil.rmtree(self.temp_dir)

    @patch("apm_cli.utils.version_checker.get_update_cache_path")
    def test_should_check_no_cache(self, mock_cache_path):
        """Test that check is needed when no cache exists."""
        mock_cache_path.return_value = self.cache_file
        self.assertTrue(should_check_for_updates())

    @patch("apm_cli.utils.version_checker.get_update_cache_path")
    def test_should_check_old_cache(self, mock_cache_path):
        """Test that check is needed when cache is old."""
        mock_cache_path.return_value = self.cache_file

        # Create cache file with old timestamp
        self.cache_file.touch()
        # Set modification time to 2 days ago
        old_time = time.time() - (2 * 86400)
        import os

        os.utime(self.cache_file, (old_time, old_time))

        self.assertTrue(should_check_for_updates())

    @patch("apm_cli.utils.version_checker.get_update_cache_path")
    def test_should_not_check_recent_cache(self, mock_cache_path):
        """Test that check is skipped when cache is recent."""
        mock_cache_path.return_value = self.cache_file

        # Create cache file with recent timestamp
        self.cache_file.touch()

        self.assertFalse(should_check_for_updates())

    @patch("apm_cli.utils.version_checker.get_update_cache_path")
    def test_save_timestamp(self, mock_cache_path):
        """Test saving check timestamp."""
        mock_cache_path.return_value = self.cache_file

        save_version_check_timestamp()

        self.assertTrue(self.cache_file.exists())


if __name__ == "__main__":
    unittest.main()
