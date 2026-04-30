"""Tests for version checker utility."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from apm_cli.utils.version_checker import (
    check_for_updates,
    get_latest_version_from_github,
    is_newer_version,
    parse_version,
    save_version_check_timestamp,
    should_check_for_updates,
)


class TestVersionParser(unittest.TestCase):
    """Test version parsing functionality."""

    def test_parse_stable_version(self):
        """Test parsing stable version strings."""
        result = parse_version("0.6.3")
        self.assertEqual(result, (0, 6, 3, ""))

        result = parse_version("1.0.0")
        self.assertEqual(result, (1, 0, 0, ""))

        result = parse_version("10.20.30")
        self.assertEqual(result, (10, 20, 30, ""))

    def test_parse_prerelease_version(self):
        """Test parsing prerelease version strings."""
        result = parse_version("0.7.0a1")
        self.assertEqual(result, (0, 7, 0, "a1"))

        result = parse_version("1.0.0b2")
        self.assertEqual(result, (1, 0, 0, "b2"))

        result = parse_version("2.0.0rc1")
        self.assertEqual(result, (2, 0, 0, "rc1"))

    def test_parse_invalid_version(self):
        """Test parsing invalid version strings."""
        self.assertIsNone(parse_version("invalid"))
        self.assertIsNone(parse_version("1.2"))
        self.assertIsNone(parse_version("1.2.3.4"))
        self.assertIsNone(parse_version("v0.6.3"))  # 'v' prefix is not accepted by parse_version
        self.assertIsNone(parse_version(""))


class TestVersionComparison(unittest.TestCase):
    """Test version comparison functionality."""

    def test_newer_major_version(self):
        """Test comparison with newer major version."""
        self.assertTrue(is_newer_version("0.6.3", "1.0.0"))
        self.assertFalse(is_newer_version("1.0.0", "0.6.3"))

    def test_newer_minor_version(self):
        """Test comparison with newer minor version."""
        self.assertTrue(is_newer_version("0.6.3", "0.7.0"))
        self.assertFalse(is_newer_version("0.7.0", "0.6.3"))

    def test_newer_patch_version(self):
        """Test comparison with newer patch version."""
        self.assertTrue(is_newer_version("0.6.3", "0.6.4"))
        self.assertFalse(is_newer_version("0.6.4", "0.6.3"))

    def test_same_version(self):
        """Test comparison with same version."""
        self.assertFalse(is_newer_version("0.6.3", "0.6.3"))
        self.assertFalse(is_newer_version("1.0.0", "1.0.0"))

    def test_prerelease_versions(self):
        """Test comparison with prerelease versions."""
        # Stable is newer than prerelease
        self.assertTrue(is_newer_version("0.6.3a1", "0.6.3"))
        self.assertFalse(is_newer_version("0.6.3", "0.6.3a1"))

        # Compare prereleases
        self.assertTrue(is_newer_version("0.6.3a1", "0.6.3a2"))
        self.assertTrue(is_newer_version("0.6.3a2", "0.6.3b1"))
        self.assertTrue(is_newer_version("0.6.3b1", "0.6.3rc1"))

    def test_invalid_versions(self):
        """Test comparison with invalid versions."""
        self.assertFalse(is_newer_version("invalid", "0.6.3"))
        self.assertFalse(is_newer_version("0.6.3", "invalid"))
        self.assertFalse(is_newer_version("invalid", "invalid"))


class TestGitHubVersionFetch(unittest.TestCase):
    """Test fetching latest version from GitHub."""

    @patch("requests.get")
    def test_fetch_successful(self, mock_get):
        """Test successful version fetch from GitHub."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"tag_name": "v0.7.0"}
        mock_get.return_value = mock_response

        result = get_latest_version_from_github()
        self.assertEqual(result, "0.7.0")

        # Verify API call
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        self.assertIn("microsoft/apm", call_args[0][0])

    @patch("requests.get")
    def test_fetch_without_v_prefix(self, mock_get):
        """Test version fetch when tag doesn't have 'v' prefix."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"tag_name": "0.7.0"}
        mock_get.return_value = mock_response

        result = get_latest_version_from_github()
        self.assertEqual(result, "0.7.0")

    @patch("requests.get")
    def test_fetch_api_error(self, mock_get):
        """Test handling of API errors."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        result = get_latest_version_from_github()
        self.assertIsNone(result)

    @patch("requests.get")
    def test_fetch_network_error(self, mock_get):
        """Test handling of network errors."""
        mock_get.side_effect = Exception("Network error")

        result = get_latest_version_from_github()
        self.assertIsNone(result)

    @patch("requests.get")
    def test_fetch_invalid_version(self, mock_get):
        """Test handling of invalid version format."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"tag_name": "invalid-version"}
        mock_get.return_value = mock_response

        result = get_latest_version_from_github()
        self.assertIsNone(result)

    @patch("builtins.__import__")
    def test_fetch_without_requests_library(self, mock_import):
        """Test behavior when requests library is not available."""

        # This test verifies graceful degradation
        def import_side_effect(name, *args, **kwargs):
            if name == "requests":
                raise ImportError("No module named 'requests'")
            return __import__(name, *args, **kwargs)

        mock_import.side_effect = import_side_effect

        result = get_latest_version_from_github()
        self.assertIsNone(result)


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


class TestCheckForUpdates(unittest.TestCase):
    """Test the main check_for_updates function."""

    @patch("apm_cli.utils.version_checker.should_check_for_updates")
    @patch("apm_cli.utils.version_checker.get_latest_version_from_github")
    @patch("apm_cli.utils.version_checker.save_version_check_timestamp")
    def test_update_available(self, mock_save, mock_fetch, mock_should_check):
        """Test when an update is available."""
        mock_should_check.return_value = True
        mock_fetch.return_value = "0.7.0"

        result = check_for_updates("0.6.3")

        self.assertEqual(result, "0.7.0")
        mock_save.assert_called_once()

    @patch("apm_cli.utils.version_checker.should_check_for_updates")
    @patch("apm_cli.utils.version_checker.get_latest_version_from_github")
    @patch("apm_cli.utils.version_checker.save_version_check_timestamp")
    def test_no_update_available(self, mock_save, mock_fetch, mock_should_check):
        """Test when no update is available."""
        mock_should_check.return_value = True
        mock_fetch.return_value = "0.6.3"

        result = check_for_updates("0.6.3")

        self.assertIsNone(result)
        mock_save.assert_called_once()

    @patch("apm_cli.utils.version_checker.should_check_for_updates")
    def test_skip_check_cached(self, mock_should_check):
        """Test that check is skipped when cached."""
        mock_should_check.return_value = False

        result = check_for_updates("0.6.3")

        self.assertIsNone(result)

    @patch("apm_cli.utils.version_checker.should_check_for_updates")
    @patch("apm_cli.utils.version_checker.get_latest_version_from_github")
    @patch("apm_cli.utils.version_checker.save_version_check_timestamp")
    def test_fetch_failure(self, mock_save, mock_fetch, mock_should_check):
        """Test handling of fetch failure."""
        mock_should_check.return_value = True
        mock_fetch.return_value = None

        result = check_for_updates("0.6.3")

        self.assertIsNone(result)
        mock_save.assert_called_once()


class TestCachePathPlatform(unittest.TestCase):
    """Test platform-specific cache path selection."""

    @patch("pathlib.Path.mkdir")
    @patch("pathlib.Path.home", return_value=Path("/home/user"))
    @patch("sys.platform", "linux")
    def test_unix_cache_path(self, mock_home, mock_mkdir):
        from apm_cli.utils.version_checker import get_update_cache_path

        result = get_update_cache_path()
        assert result == Path("/home/user") / ".cache" / "apm" / "last_version_check"

    @patch("pathlib.Path.mkdir")
    @patch("pathlib.Path.home", return_value=Path("C:/Users/testuser"))
    @patch("sys.platform", "win32")
    def test_windows_cache_path(self, mock_home, mock_mkdir):
        from apm_cli.utils.version_checker import get_update_cache_path

        result = get_update_cache_path()
        assert (
            result
            == Path("C:/Users/testuser")
            / "AppData"
            / "Local"
            / "apm"
            / "cache"
            / "last_version_check"
        )


if __name__ == "__main__":
    unittest.main()
