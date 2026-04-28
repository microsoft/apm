"""Tests for apm_cli.policy.discovery — policy auto-discovery engine."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.policy.discovery import (
    CACHE_SCHEMA_VERSION,
    DEFAULT_CACHE_TTL,
    MAX_STALE_TTL,
    PolicyFetchResult,
    _auto_discover,
    _cache_key,
    _extract_org_from_git_remote,
    _fetch_from_repo,
    _fetch_from_url,
    _fetch_github_contents,
    _get_cache_dir,
    _load_from_file,
    _parse_remote_url,
    _read_cache,
    _write_cache,
    discover_policy,
)
from apm_cli.policy.parser import PolicyValidationError, load_policy
from apm_cli.policy.schema import ApmPolicy

# Minimal valid YAML that produces a valid ApmPolicy
VALID_POLICY_YAML = "name: test-policy\nversion: '1.0'\nenforcement: warn\n"


def _make_test_policy(yaml_str: str = VALID_POLICY_YAML) -> ApmPolicy:
    """Parse YAML string into an ApmPolicy for test setup."""
    policy, _ = load_policy(yaml_str)
    return policy


class TestParseRemoteUrl(unittest.TestCase):
    """Test _parse_remote_url for various git remote formats."""

    def test_https_github(self):
        result = _parse_remote_url("https://github.com/contoso/my-project.git")
        self.assertEqual(result, ("contoso", "github.com"))

    def test_ssh_github(self):
        result = _parse_remote_url("git@github.com:contoso/my-project.git")
        self.assertEqual(result, ("contoso", "github.com"))

    def test_https_ghe(self):
        result = _parse_remote_url(
            "https://github.example.com/contoso/my-project.git"
        )
        self.assertEqual(result, ("contoso", "github.example.com"))

    def test_ado(self):
        result = _parse_remote_url(
            "https://dev.azure.com/contoso/project/_git/repo"
        )
        self.assertEqual(result, ("contoso", "dev.azure.com"))

    def test_ssh_no_git_suffix(self):
        result = _parse_remote_url("git@github.com:contoso/my-project")
        self.assertEqual(result, ("contoso", "github.com"))

    def test_https_no_git_suffix(self):
        result = _parse_remote_url("https://github.com/contoso/my-project")
        self.assertEqual(result, ("contoso", "github.com"))

    def test_https_trailing_slash(self):
        result = _parse_remote_url("https://github.com/contoso/my-project/")
        self.assertEqual(result, ("contoso", "github.com"))

    def test_ssh_trailing_slash(self):
        result = _parse_remote_url("git@github.com:contoso/my-project/")
        self.assertEqual(result, ("contoso", "github.com"))

    def test_empty_string(self):
        result = _parse_remote_url("")
        self.assertIsNone(result)

    def test_invalid_url(self):
        result = _parse_remote_url("not-a-url")
        self.assertIsNone(result)

    def test_ssh_empty_path(self):
        result = _parse_remote_url("git@github.com:")
        self.assertIsNone(result)

    def test_https_no_path(self):
        result = _parse_remote_url("https://github.com/")
        self.assertIsNone(result)


class TestExtractOrgFromGitRemote(unittest.TestCase):
    """Test _extract_org_from_git_remote with mocked subprocess."""

    @patch("apm_cli.policy.discovery.subprocess.run")
    def test_successful_remote(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/contoso/my-project.git\n",
        )
        result = _extract_org_from_git_remote(Path("/fake"))
        self.assertEqual(result, ("contoso", "github.com"))
        mock_run.assert_called_once_with(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=Path("/fake"),
            timeout=5,
        )

    @patch("apm_cli.policy.discovery.subprocess.run")
    def test_git_command_fails(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = _extract_org_from_git_remote(Path("/fake"))
        self.assertIsNone(result)

    @patch("apm_cli.policy.discovery.subprocess.run")
    def test_git_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("git not found")
        result = _extract_org_from_git_remote(Path("/fake"))
        self.assertIsNone(result)

    @patch("apm_cli.policy.discovery.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=5)
        result = _extract_org_from_git_remote(Path("/fake"))
        self.assertIsNone(result)


class TestLoadFromFile(unittest.TestCase):
    """Test _load_from_file with real filesystem."""

    def test_valid_policy_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "policy.yml"
            p.write_text(VALID_POLICY_YAML, encoding="utf-8")
            result = _load_from_file(p)
            self.assertTrue(result.found)
            self.assertIsInstance(result.policy, ApmPolicy)
            self.assertEqual(result.policy.name, "test-policy")
            self.assertIn("file:", result.source)
            self.assertIsNone(result.error)

    def test_invalid_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "bad-policy.yml"
            p.write_text("enforcement: invalid-value\n", encoding="utf-8")
            result = _load_from_file(p)
            self.assertFalse(result.found)
            self.assertIsNotNone(result.error)
            self.assertIn("Invalid policy file", result.error)

    def test_unreadable_file(self):
        result = _load_from_file(Path("/nonexistent/file.yml"))
        self.assertFalse(result.found)
        self.assertIsNotNone(result.error)


class TestCacheReadWrite(unittest.TestCase):
    """Test cache read/write operations with real filesystem."""

    def test_write_then_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "contoso/.github"

            _write_cache(repo_ref, _make_test_policy(), root)

            result = _read_cache(repo_ref, root)
            self.assertIsNotNone(result)
            self.assertTrue(result.found)
            self.assertTrue(result.cached)
            self.assertEqual(result.source, f"org:{repo_ref}")

    def test_expired_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "contoso/.github"

            _write_cache(repo_ref, _make_test_policy(), root)

            # Backdate the metadata to make it expired
            cache_dir = _get_cache_dir(root)
            key = _cache_key(repo_ref)
            meta_file = cache_dir / f"{key}.meta.json"
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            meta["cached_at"] = time.time() - DEFAULT_CACHE_TTL - 100
            meta_file.write_text(json.dumps(meta), encoding="utf-8")

            result = _read_cache(repo_ref, root)
            self.assertIsNone(result)

    def test_missing_cache_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _read_cache("nonexistent/ref", Path(tmpdir))
            self.assertIsNone(result)

    def test_corrupted_meta_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "contoso/.github"

            _write_cache(repo_ref, _make_test_policy(), root)

            # Corrupt the meta file
            cache_dir = _get_cache_dir(root)
            key = _cache_key(repo_ref)
            meta_file = cache_dir / f"{key}.meta.json"
            meta_file.write_text("not valid json", encoding="utf-8")

            result = _read_cache(repo_ref, root)
            self.assertIsNone(result)

    def test_cache_key_deterministic(self):
        key1 = _cache_key("contoso/.github")
        key2 = _cache_key("contoso/.github")
        self.assertEqual(key1, key2)

    def test_cache_key_different_refs(self):
        key1 = _cache_key("contoso/.github")
        key2 = _cache_key("fabrikam/.github")
        self.assertNotEqual(key1, key2)

    def test_get_cache_dir(self):
        root = Path("/fake/project")
        # _get_cache_dir resolves project_root (#886), compare
        # against the resolved form
        expected = root.resolve() / "apm_modules" / ".policy-cache"
        self.assertEqual(_get_cache_dir(root), expected)


class TestFetchGithubContents(unittest.TestCase):
    """Test _fetch_github_contents with mocked requests."""

    def _b64_response(self, content: str) -> dict:
        """Create a GitHub API response with base64-encoded content."""
        return {
            "encoding": "base64",
            "content": base64.b64encode(content.encode()).decode(),
        }

    @patch("apm_cli.policy.discovery._get_token_for_host", return_value=None)
    @patch("apm_cli.policy.discovery.requests")
    def test_200_base64_content(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = self._b64_response(VALID_POLICY_YAML)
        mock_requests.get.return_value = mock_resp

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(error)
        self.assertEqual(content, VALID_POLICY_YAML)

    @patch("apm_cli.policy.discovery._get_token_for_host", return_value=None)
    @patch("apm_cli.policy.discovery.requests")
    def test_200_plain_content(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"content": VALID_POLICY_YAML}
        mock_requests.get.return_value = mock_resp

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(error)
        self.assertEqual(content, VALID_POLICY_YAML)

    @patch("apm_cli.policy.discovery._get_token_for_host", return_value=None)
    @patch("apm_cli.policy.discovery.requests")
    def test_404(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_requests.get.return_value = mock_resp

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("404", error)

    @patch("apm_cli.policy.discovery._get_token_for_host", return_value=None)
    @patch("apm_cli.policy.discovery.requests")
    def test_403(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_requests.get.return_value = mock_resp

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("403", error)

    @patch("apm_cli.policy.discovery._get_token_for_host", return_value=None)
    @patch("apm_cli.policy.discovery.requests")
    def test_timeout(self, mock_requests, _mock_token):
        import requests as real_requests

        mock_requests.exceptions = real_requests.exceptions
        mock_requests.get.side_effect = real_requests.exceptions.Timeout()

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("Timeout", error)

    @patch("apm_cli.policy.discovery._get_token_for_host", return_value=None)
    @patch("apm_cli.policy.discovery.requests")
    def test_connection_error(self, mock_requests, _mock_token):
        import requests as real_requests

        mock_requests.exceptions = real_requests.exceptions
        mock_requests.get.side_effect = real_requests.exceptions.ConnectionError()

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("Connection error", error)

    @patch("apm_cli.policy.discovery._get_token_for_host", return_value=None)
    @patch("apm_cli.policy.discovery.requests")
    def test_unexpected_response_format(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"type": "dir"}
        mock_requests.get.return_value = mock_resp

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("Unexpected response", error)

    @patch("apm_cli.policy.discovery._get_token_for_host", return_value=None)
    @patch("apm_cli.policy.discovery.requests")
    def test_invalid_repo_ref(self, mock_requests, _mock_token):
        content, error = _fetch_github_contents("invalid", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("Invalid repo reference", error)

    @patch("apm_cli.policy.discovery._get_token_for_host", return_value="ghp_test123")
    @patch("apm_cli.policy.discovery.requests")
    def test_auth_header_sent(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = self._b64_response(VALID_POLICY_YAML)
        mock_requests.get.return_value = mock_resp

        _fetch_github_contents("contoso/.github", "apm-policy.yml")

        call_kwargs = mock_requests.get.call_args[1]
        self.assertIn("Authorization", call_kwargs["headers"])
        self.assertEqual(call_kwargs["headers"]["Authorization"], "token ghp_test123")

    @patch("apm_cli.policy.discovery._get_token_for_host", return_value=None)
    @patch("apm_cli.policy.discovery.requests")
    def test_ghe_api_url(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_requests.get.return_value = mock_resp

        _fetch_github_contents(
            "ghe.example.com/contoso/.github", "apm-policy.yml"
        )

        call_url = mock_requests.get.call_args[0][0]
        self.assertTrue(
            call_url.startswith("https://ghe.example.com/api/v3/repos/")
        )


class TestFetchFromRepo(unittest.TestCase):
    """Test _fetch_from_repo combining API fetch and cache."""

    @patch("apm_cli.policy.discovery._fetch_github_contents")
    def test_200_caches_result(self, mock_fetch):
        mock_fetch.return_value = (VALID_POLICY_YAML, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = _fetch_from_repo("contoso/.github", root, no_cache=True)
            self.assertTrue(result.found)
            self.assertEqual(result.source, "org:contoso/.github")
            self.assertFalse(result.cached)

    @patch("apm_cli.policy.discovery._fetch_github_contents")
    def test_404_no_error(self, mock_fetch):
        mock_fetch.return_value = (None, "404: Policy file not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_repo(
                "contoso/.github", Path(tmpdir), no_cache=True
            )
            self.assertFalse(result.found)
            self.assertIsNone(result.error)  # 404 is not an error

    @patch("apm_cli.policy.discovery._fetch_github_contents")
    def test_api_error(self, mock_fetch):
        mock_fetch.return_value = (None, "Connection error fetching policy")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_repo(
                "contoso/.github", Path(tmpdir), no_cache=True
            )
            self.assertFalse(result.found)
            self.assertIsNotNone(result.error)

    @patch("apm_cli.policy.discovery._fetch_github_contents")
    def test_invalid_policy_yaml(self, mock_fetch):
        mock_fetch.return_value = ("enforcement: bogus\n", None)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_repo(
                "contoso/.github", Path(tmpdir), no_cache=True
            )
            self.assertFalse(result.found)
            self.assertIn("Invalid policy", result.error)

    def test_cache_hit_skips_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "contoso/.github"
            _write_cache(repo_ref, _make_test_policy(), root)

            # Should hit cache, no API call needed
            result = _fetch_from_repo(repo_ref, root, no_cache=False)
            self.assertTrue(result.found)
            self.assertTrue(result.cached)


class TestFetchFromUrl(unittest.TestCase):
    """Test _fetch_from_url with mocked requests."""

    @patch("apm_cli.policy.discovery.requests")
    def test_200_success(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = VALID_POLICY_YAML
        mock_requests.get.return_value = mock_resp
        mock_requests.exceptions = __import__("requests").exceptions

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_url(
                "https://example.com/policy.yml", Path(tmpdir), no_cache=True
            )
            self.assertTrue(result.found)
            self.assertEqual(result.source, "url:https://example.com/policy.yml")

    @patch("apm_cli.policy.discovery.requests")
    def test_404(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_requests.get.return_value = mock_resp
        mock_requests.exceptions = __import__("requests").exceptions

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_url(
                "https://example.com/policy.yml", Path(tmpdir), no_cache=True
            )
            self.assertFalse(result.found)
            self.assertIn("404", result.error)

    @patch("apm_cli.policy.discovery.requests")
    def test_timeout(self, mock_requests):
        import requests as real_requests

        mock_requests.exceptions = real_requests.exceptions
        mock_requests.get.side_effect = real_requests.exceptions.Timeout()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_url(
                "https://example.com/policy.yml", Path(tmpdir), no_cache=True
            )
            self.assertFalse(result.found)
            self.assertIn("Timeout", result.error)

    @patch("apm_cli.policy.discovery.requests")
    def test_invalid_policy_content(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "enforcement: bogus\n"
        mock_requests.get.return_value = mock_resp
        mock_requests.exceptions = __import__("requests").exceptions

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_url(
                "https://example.com/policy.yml", Path(tmpdir), no_cache=True
            )
            self.assertFalse(result.found)
            self.assertIn("Invalid policy", result.error)


class TestDiscoverPolicy(unittest.TestCase):
    """Integration-level tests for discover_policy."""

    def test_override_local_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "override-policy.yml"
            p.write_text(VALID_POLICY_YAML, encoding="utf-8")
            result = discover_policy(
                Path("/fake"), policy_override=str(p)
            )
            self.assertTrue(result.found)
            self.assertIn("file:", result.source)

    @patch("apm_cli.policy.discovery.requests")
    def test_override_url(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = VALID_POLICY_YAML
        mock_requests.get.return_value = mock_resp
        mock_requests.exceptions = __import__("requests").exceptions

        with tempfile.TemporaryDirectory() as tmpdir:
            result = discover_policy(
                Path(tmpdir),
                policy_override="https://example.com/policy.yml",
                no_cache=True,
            )
            self.assertTrue(result.found)
            self.assertIn("url:", result.source)

    @patch("apm_cli.policy.discovery._fetch_github_contents")
    def test_override_owner_repo(self, mock_fetch):
        mock_fetch.return_value = (VALID_POLICY_YAML, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = discover_policy(
                Path(tmpdir),
                policy_override="contoso/.github",
                no_cache=True,
            )
            self.assertTrue(result.found)
            self.assertIn("org:", result.source)

    @patch("apm_cli.policy.discovery._fetch_github_contents")
    @patch("apm_cli.policy.discovery.subprocess.run")
    def test_override_org_auto_discovers(self, mock_run, mock_fetch):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/contoso/my-project.git\n",
        )
        mock_fetch.return_value = (VALID_POLICY_YAML, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = discover_policy(
                Path(tmpdir), policy_override="org", no_cache=True
            )
            self.assertTrue(result.found)
            mock_fetch.assert_called_once()

    @patch("apm_cli.policy.discovery._fetch_github_contents")
    @patch("apm_cli.policy.discovery.subprocess.run")
    def test_none_auto_discovers(self, mock_run, mock_fetch):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/contoso/my-project.git\n",
        )
        mock_fetch.return_value = (VALID_POLICY_YAML, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = discover_policy(Path(tmpdir), no_cache=True)
            self.assertTrue(result.found)
            self.assertEqual(result.source, "org:contoso/.github")

    @patch("apm_cli.policy.discovery.subprocess.run")
    def test_no_git_remote(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = discover_policy(Path(tmpdir), no_cache=True)
            self.assertFalse(result.found)
            self.assertIn("Could not determine org", result.error)

    @patch("apm_cli.policy.discovery._fetch_github_contents")
    @patch("apm_cli.policy.discovery.subprocess.run")
    def test_cache_hit_returns_cached(self, mock_run, mock_fetch):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/contoso/my-project.git\n",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Pre-populate cache
            _write_cache("contoso/.github", _make_test_policy(), root)

            result = discover_policy(root, no_cache=False)
            self.assertTrue(result.found)
            self.assertTrue(result.cached)
            mock_fetch.assert_not_called()

    @patch("apm_cli.policy.discovery._fetch_github_contents")
    @patch("apm_cli.policy.discovery.subprocess.run")
    def test_ghe_repo_ref_includes_host(self, mock_run, mock_fetch):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://ghe.example.com/contoso/my-project.git\n",
        )
        mock_fetch.return_value = (VALID_POLICY_YAML, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = discover_policy(Path(tmpdir), no_cache=True)
            self.assertTrue(result.found)
            self.assertEqual(
                result.source, "org:ghe.example.com/contoso/.github"
            )


class TestAutoDiscover(unittest.TestCase):
    """Test _auto_discover logic."""

    @patch("apm_cli.policy.discovery._fetch_from_repo")
    @patch("apm_cli.policy.discovery._extract_org_from_git_remote")
    def test_github_com_repo_ref(self, mock_extract, mock_fetch):
        mock_extract.return_value = ("contoso", "github.com")
        mock_fetch.return_value = PolicyFetchResult(
            policy=ApmPolicy(), source="org:contoso/.github"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            mock_fetch.assert_called_once_with(
                "contoso/.github", Path(tmpdir), no_cache=True, expected_hash=None
            )
            self.assertTrue(result.found)

    @patch("apm_cli.policy.discovery._fetch_from_repo")
    @patch("apm_cli.policy.discovery._extract_org_from_git_remote")
    def test_ghe_repo_ref_includes_host(self, mock_extract, mock_fetch):
        mock_extract.return_value = ("contoso", "ghe.example.com")
        mock_fetch.return_value = PolicyFetchResult(
            policy=ApmPolicy(), source="org:ghe.example.com/contoso/.github"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            _auto_discover(Path(tmpdir), no_cache=True)
            mock_fetch.assert_called_once_with(
                "ghe.example.com/contoso/.github",
                Path(tmpdir),
                no_cache=True,
                expected_hash=None,
            )

    @patch("apm_cli.policy.discovery._extract_org_from_git_remote")
    def test_no_remote_returns_error(self, mock_extract):
        mock_extract.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            self.assertFalse(result.found)
            self.assertIn("Could not determine org", result.error)


class TestGetTokenForHost(unittest.TestCase):
    """Test _get_token_for_host delegation."""

    @patch.dict(os.environ, {"GITHUB_TOKEN": "test-tok"}, clear=False)
    @patch(
        "apm_cli.core.token_manager.GitHubTokenManager.get_token_with_credential_fallback",
        side_effect=Exception("simulated failure"),
    )
    def test_fallback_to_env_vars(self, _mock_method):
        from apm_cli.policy.discovery import _get_token_for_host

        token = _get_token_for_host("github.com")
        self.assertEqual(token, "test-tok")

    @patch.dict(
        os.environ,
        {"GITHUB_TOKEN": "", "GITHUB_APM_PAT": "", "GH_TOKEN": ""},
        clear=False,
    )
    @patch(
        "apm_cli.core.token_manager.GitHubTokenManager.get_token_with_credential_fallback",
        side_effect=Exception("simulated failure"),
    )
    def test_no_token_available(self, _mock_method):
        from apm_cli.policy.discovery import _get_token_for_host

        token = _get_token_for_host("github.com")
        # All env vars are empty strings, which are falsy
        self.assertFalse(token)


class TestPolicyFetchResult(unittest.TestCase):
    """Test PolicyFetchResult dataclass."""

    def test_found_with_policy(self):
        result = PolicyFetchResult(policy=ApmPolicy())
        self.assertTrue(result.found)

    def test_not_found_without_policy(self):
        result = PolicyFetchResult()
        self.assertFalse(result.found)

    def test_defaults(self):
        result = PolicyFetchResult()
        self.assertIsNone(result.policy)
        self.assertEqual(result.source, "")
        self.assertFalse(result.cached)
        self.assertIsNone(result.error)


if __name__ == "__main__":
    unittest.main()
