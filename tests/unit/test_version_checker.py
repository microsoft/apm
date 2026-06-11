"""Tests for version checker utility."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from apm_cli.utils.version_checker import (
    _get_github_token,
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


class TestGitHubTokenResolution(unittest.TestCase):
    """Test GitHub token resolution helper."""

    @patch.dict("os.environ", {}, clear=True)
    def test_no_token_when_env_empty(self):
        """Returns None when no token env vars are set."""
        for var in ("GITHUB_APM_PAT", "GITHUB_TOKEN", "GH_TOKEN"):
            self.assertNotIn(var, __import__("os").environ)
        self.assertIsNone(_get_github_token())

    @patch.dict("os.environ", {"GITHUB_APM_PAT": "pat_value"}, clear=False)
    def test_prefers_github_apm_pat(self):
        """GITHUB_APM_PAT is chosen over GITHUB_TOKEN and GH_TOKEN."""
        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghtok", "GH_TOKEN": "gh"}):
            token = _get_github_token()
        self.assertEqual(token, "pat_value")

    @patch.dict("os.environ", {"GITHUB_TOKEN": "ghtok"}, clear=False)
    def test_falls_back_to_github_token(self):
        """Falls back to GITHUB_TOKEN when GITHUB_APM_PAT is absent."""
        import os

        env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_APM_PAT", "GH_TOKEN")}
        env["GITHUB_TOKEN"] = "ghtok"
        with patch.dict("os.environ", env, clear=True):
            token = _get_github_token()
        self.assertEqual(token, "ghtok")

    @patch.dict("os.environ", {"GH_TOKEN": "gh_value"}, clear=True)
    def test_falls_back_to_gh_token(self):
        """Falls back to GH_TOKEN as last resort."""
        token = _get_github_token()
        self.assertEqual(token, "gh_value")

    @patch.dict("os.environ", {}, clear=True)
    @patch("apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git")
    @patch("apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_gh_cli")
    def test_version_check_token_resolution_does_not_probe_external_helpers(
        self, mock_gh_cli, mock_git
    ):
        """Version checks use AuthResolver without gh/git credential probing."""
        mock_gh_cli.side_effect = AssertionError("gh cli should not be probed")
        mock_git.side_effect = AssertionError("git credentials should not be probed")

        self.assertIsNone(_get_github_token())
        mock_gh_cli.assert_not_called()
        mock_git.assert_not_called()

    @patch.dict("os.environ", {"GITHUB_TOKEN": "resolver_token"}, clear=True)
    @patch("apm_cli.utils.version_checker.AuthResolver")
    def test_version_check_token_resolution_uses_auth_resolver(self, mock_resolver_cls):
        """Version checks resolve GitHub tokens through AuthResolver."""
        mock_context = Mock(token="resolver_token")
        mock_resolver = Mock()
        mock_resolver.resolve.return_value = mock_context
        mock_resolver_cls.return_value = mock_resolver

        self.assertEqual(_get_github_token(), "resolver_token")
        mock_resolver.resolve.assert_called_once()


class TestGitHubVersionFetchAuth(unittest.TestCase):
    """Test that get_latest_version_from_github sends auth headers correctly."""

    @patch("apm_cli.utils.version_checker._get_github_token", return_value=None)
    @patch("requests.get")
    def test_no_auth_header_when_no_token(self, mock_get, mock_token):
        """No Authorization header is sent when no token is present."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"tag_name": "v0.8.0"}
        mock_get.return_value = mock_response

        get_latest_version_from_github()

        call_kwargs = mock_get.call_args[1]
        headers = call_kwargs.get("headers", {})
        self.assertNotIn("Authorization", headers)

    @patch("apm_cli.utils.version_checker._get_github_token", return_value="my_secret_token")
    @patch("requests.get")
    def test_auth_header_sent_when_token_present(self, mock_get, mock_token):
        """Authorization header IS sent when a token is available."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"tag_name": "v0.8.0"}
        mock_get.return_value = mock_response

        get_latest_version_from_github()

        call_kwargs = mock_get.call_args[1]
        headers = call_kwargs.get("headers", {})
        self.assertIn("Authorization", headers)
        self.assertTrue(headers["Authorization"].startswith("token "))

    @patch("apm_cli.utils.version_checker._get_github_token", return_value="mirror_secret")
    @patch("requests.get")
    def test_token_header_scoped_to_public_release_metadata_request(self, mock_get, mock_token):
        """Token headers are only attached to non-mirrored release metadata requests."""
        from urllib.parse import urlparse

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"tag_name": "v0.8.0"}
        mock_get.return_value = mock_response

        mirrored_env = {
            "APM_RELEASE_METADATA_URL": "https://mirror.corp.example/apm/latest.json",
            "GITHUB_TOKEN": "mirror_secret",
        }
        with patch.dict("os.environ", mirrored_env, clear=True):
            get_latest_version_from_github()

        mirrored_url = mock_get.call_args[0][0]
        mirrored_headers = mock_get.call_args[1].get("headers", {})
        mirrored_parsed = urlparse(mirrored_url)
        self.assertEqual(mirrored_parsed.scheme, "https")
        self.assertEqual(mirrored_parsed.hostname, "mirror.corp.example")
        self.assertEqual(mirrored_parsed.path, "/apm/latest.json")
        self.assertNotIn("Authorization", mirrored_headers)

        mock_get.reset_mock()
        default_env = {"GITHUB_TOKEN": "mirror_secret"}
        with patch.dict("os.environ", default_env, clear=True):
            get_latest_version_from_github()

        default_url = mock_get.call_args[0][0]
        default_headers = mock_get.call_args[1].get("headers", {})
        default_parsed = urlparse(default_url)
        self.assertEqual(default_parsed.scheme, "https")
        self.assertEqual(default_parsed.hostname, "api.github.com")
        self.assertEqual(default_parsed.path, "/repos/microsoft/apm/releases/latest")
        self.assertEqual(default_headers.get("Authorization"), "token mirror_secret")

    @patch("apm_cli.utils.version_checker._get_github_token", return_value="my_secret_token")
    @patch("requests.get")
    def test_token_value_not_in_exception_text(self, mock_get, mock_token):
        """Token value must not appear in any raised exception or return value."""
        mock_get.side_effect = Exception("connection refused")

        import io
        import logging

        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        root = logging.getLogger()
        root.addHandler(handler)

        result = get_latest_version_from_github()

        root.removeHandler(handler)
        log_output = log_capture.getvalue()

        self.assertIsNone(result)
        self.assertNotIn("my_secret_token", log_output)

    @patch("apm_cli.utils.version_checker._get_github_token", return_value=None)
    @patch("requests.get")
    def test_rate_limit_403_returns_none_without_token(self, mock_get, mock_token):
        """A 403 rate-limit response with no token returns None gracefully."""
        mock_response = Mock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        result = get_latest_version_from_github()
        self.assertIsNone(result)

    @patch("apm_cli.utils.version_checker._get_github_token", return_value="valid_token")
    @patch("requests.get")
    def test_200_with_token_returns_version(self, mock_get, mock_token):
        """A 200 response when token is present returns the version string."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"tag_name": "v1.0.0"}
        mock_get.return_value = mock_response

        result = get_latest_version_from_github()
        self.assertEqual(result, "1.0.0")


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


class TestAirGappedEnvVars(unittest.TestCase):
    """Test that air-gapped env vars are honoured by the version checker."""

    @patch("requests.get")
    @patch.dict("os.environ", {"VERSION": "v1.2.3"}, clear=False)
    def test_version_env_var_skips_api_call(self, mock_get):
        """When VERSION is set the API is never called and the pinned version is returned."""
        result = get_latest_version_from_github()
        mock_get.assert_not_called()
        self.assertEqual(result, "1.2.3")

    @patch("requests.get")
    def test_release_metadata_url_overrides_github_api_url(self, mock_get):
        """APM_RELEASE_METADATA_URL targets mirror metadata instead of GitHub's API."""
        from urllib.parse import urlparse

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"tag_name": "v2.0.0"}
        mock_get.return_value = mock_response

        import os as _os

        env = {
            k: v
            for k, v in _os.environ.items()
            if k not in ("VERSION", "GITHUB_URL", "APM_REPO", "APM_RELEASE_METADATA_URL")
        }
        env["APM_RELEASE_METADATA_URL"] = "https://mirror.corp.example/apm/latest.json"
        with patch.dict("os.environ", env, clear=True):
            result = get_latest_version_from_github()

        self.assertEqual(result, "2.0.0")
        call_url = mock_get.call_args[0][0]
        parsed = urlparse(call_url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "mirror.corp.example")
        self.assertEqual(parsed.path, "/apm/latest.json")

    @patch("requests.get")
    def test_no_direct_fallback_without_mirror_skips_public_request(self, mock_get):
        """APM_NO_DIRECT_FALLBACK avoids public metadata requests when no mirror is set."""
        import os as _os

        env = {
            k: v
            for k, v in _os.environ.items()
            if k
            not in (
                "VERSION",
                "GITHUB_URL",
                "APM_REPO",
                "APM_RELEASE_METADATA_URL",
                "APM_NO_DIRECT_FALLBACK",
            )
        }
        env["APM_NO_DIRECT_FALLBACK"] = "1"
        with patch.dict("os.environ", env, clear=True):
            result = get_latest_version_from_github()

        self.assertIsNone(result)
        mock_get.assert_not_called()

    @patch("requests.get")
    @patch.dict("os.environ", {"VERSION": "1.5.0"}, clear=False)
    def test_version_env_var_without_v_prefix(self, mock_get):
        """VERSION without 'v' prefix is accepted and API is skipped."""
        result = get_latest_version_from_github()
        mock_get.assert_not_called()
        self.assertEqual(result, "1.5.0")

    @patch("requests.get")
    @patch.dict("os.environ", {"VERSION": "not-a-version"}, clear=False)
    def test_invalid_version_env_var_returns_none(self, mock_get):
        """An invalid VERSION value returns None without calling the API."""
        result = get_latest_version_from_github()
        mock_get.assert_not_called()
        self.assertIsNone(result)

    @patch("requests.get")
    @patch.dict("os.environ", {"APM_REPO": "corp/apm-fork"}, clear=False)
    def test_apm_repo_env_var_used_in_api_url(self, mock_get):
        """When APM_REPO is set, the API request targets that repository."""
        from urllib.parse import urlparse

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"tag_name": "v0.9.0"}
        mock_get.return_value = mock_response

        # Remove VERSION so it does not short-circuit
        import os as _os

        env = {k: v for k, v in _os.environ.items() if k != "VERSION"}
        with patch.dict("os.environ", env, clear=True):
            with patch.dict("os.environ", {"APM_REPO": "corp/apm-fork"}):
                result = get_latest_version_from_github()

        self.assertEqual(result, "0.9.0")
        call_url = mock_get.call_args[0][0]
        parsed = urlparse(call_url)
        self.assertIn("corp/apm-fork", parsed.path)

    @patch("requests.get")
    def test_github_url_env_var_uses_ghe_api_endpoint(self, mock_get):
        """When GITHUB_URL is set to a GHE host, the API URL uses /api/v3."""
        from urllib.parse import urlparse

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"tag_name": "v0.9.0"}
        mock_get.return_value = mock_response

        import os as _os

        env = {k: v for k, v in _os.environ.items() if k not in ("VERSION", "GITHUB_URL")}
        with patch.dict("os.environ", env, clear=True):
            with patch.dict("os.environ", {"GITHUB_URL": "https://gh.corp.com"}):
                result = get_latest_version_from_github()

        self.assertEqual(result, "0.9.0")
        call_url = mock_get.call_args[0][0]
        parsed = urlparse(call_url)
        self.assertEqual(parsed.hostname, "gh.corp.com")
        self.assertTrue(parsed.path.startswith("/api/v3/"))

    @patch("requests.get")
    def test_default_behavior_without_env_vars(self, mock_get):
        """Without env vars, the public api.github.com endpoint is used."""
        from urllib.parse import urlparse

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"tag_name": "v0.9.0"}
        mock_get.return_value = mock_response

        import os as _os

        env = {
            k: v for k, v in _os.environ.items() if k not in ("VERSION", "GITHUB_URL", "APM_REPO")
        }
        with patch.dict("os.environ", env, clear=True):
            result = get_latest_version_from_github()

        self.assertEqual(result, "0.9.0")
        call_url = mock_get.call_args[0][0]
        parsed = urlparse(call_url)
        self.assertEqual(parsed.hostname, "api.github.com")
        self.assertIn("microsoft/apm", parsed.path)
