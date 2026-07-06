"""Tests for apm_cli.policy.discovery  --  policy auto-discovery engine."""

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
    CACHE_SCHEMA_VERSION,  # noqa: F401
    DEFAULT_CACHE_TTL,
    MAX_STALE_TTL,  # noqa: F401
    PolicyFetchResult,
    _auto_discover,
    _cache_key,
    _extract_org_from_git_remote,
    _fetch_ado_contents,
    _fetch_from_ado_repo,
    _fetch_from_repo,
    _fetch_from_url,
    _fetch_github_contents,
    _get_cache_dir,
    _load_from_file,
    _parse_remote_url,
    _policy_repo_candidates,
    _read_cache,
    _write_cache,
    discover_policy,
)
from apm_cli.policy.parser import PolicyValidationError, load_policy  # noqa: F401
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
        result = _parse_remote_url("https://github.example.com/contoso/my-project.git")
        self.assertEqual(result, ("contoso", "github.example.com"))

    def test_ado(self):
        result = _parse_remote_url("https://dev.azure.com/contoso/project/_git/repo")
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

    def test_https_visualstudio_uses_org_subdomain(self):
        result = _parse_remote_url("https://contoso.visualstudio.com/project/_git/repo")
        self.assertEqual(result, ("contoso", "contoso.visualstudio.com"))

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

    # --- Regression: #1159 SCP non-`git` user (EMU / GHE) ---

    def test_scp_emu_enterprise_user(self):
        """SCP-like SSH with non-`git` user (EMU/GHE) must parse, not return None."""
        result = _parse_remote_url("enterprise-user@ghe.corp.com:contoso/my-project.git")
        self.assertEqual(result, ("contoso", "ghe.corp.com"))

    def test_scp_custom_user(self):
        """SCP-like SSH with arbitrary username parses correctly."""
        result = _parse_remote_url("alice@github.example.com:org/repo.git")
        self.assertEqual(result, ("org", "github.example.com"))

    def test_scp_user_with_dot_dash(self):
        """SCP usernames may include `.` `-` `_` `+` -- still parse."""
        result = _parse_remote_url("first.last-1@github.com:contoso/repo.git")
        self.assertEqual(result, ("contoso", "github.com"))

    def test_ado_ssh_v3_prefix(self):
        """Azure DevOps SSH URLs carry a `v3/` segment that is NOT the org."""
        result = _parse_remote_url("git@ssh.dev.azure.com:v3/myorg/myproject/myrepo")
        self.assertEqual(result, ("myorg", "ssh.dev.azure.com"))

    def test_ado_ssh_v3_prefix_with_git_suffix(self):
        result = _parse_remote_url("git@ssh.dev.azure.com:v3/myorg/myproject/myrepo.git")
        self.assertEqual(result, ("myorg", "ssh.dev.azure.com"))


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

    def test_round_trip_preserves_none_deny_and_require(self):
        """Cache write->read must preserve deny=None/require=None (tri-state Fix 1).

        A policy with no dependencies: block must survive a cache round-trip
        as None, not collapse to () which would prevent parent inheritance.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "contoso/.github"

            # Policy with no dependencies: block -> deny=None, require=None
            policy, _ = load_policy("name: p\nversion: '1'\nenforcement: warn\n")
            self.assertIsNone(policy.dependencies.deny)
            self.assertIsNone(policy.dependencies.require)

            _write_cache(repo_ref, policy, root)
            result = _read_cache(repo_ref, root)

            self.assertIsNotNone(result)
            self.assertIsNone(
                result.policy.dependencies.deny,
                "deny must survive cache round-trip as None, not collapse to ()",
            )
            self.assertIsNone(
                result.policy.dependencies.require,
                "require must survive cache round-trip as None, not collapse to ()",
            )

    def test_round_trip_preserves_explicit_empty_deny(self):
        """Cache round-trip must preserve deny=() (explicit empty override)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "contoso/.github"

            yaml_str = "name: p\nversion: '1'\nenforcement: warn\ndependencies:\n  deny: []\n"
            policy, _ = load_policy(yaml_str)
            self.assertEqual(policy.dependencies.deny, ())

            _write_cache(repo_ref, policy, root)
            result = _read_cache(repo_ref, root)

            self.assertIsNotNone(result)
            self.assertEqual(
                result.policy.dependencies.deny,
                (),
                "deny=[] must survive cache round-trip as () (explicit empty)",
            )


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

        _fetch_github_contents("ghe.example.com/contoso/.github", "apm-policy.yml")

        call_url = mock_requests.get.call_args[0][0]
        self.assertTrue(call_url.startswith("https://ghe.example.com/api/v3/repos/"))


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
            result = _fetch_from_repo("contoso/.github", Path(tmpdir), no_cache=True)
            self.assertFalse(result.found)
            self.assertIsNone(result.error)  # 404 is not an error

    @patch("apm_cli.policy.discovery._fetch_github_contents")
    def test_api_error(self, mock_fetch):
        mock_fetch.return_value = (None, "Connection error fetching policy")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_repo("contoso/.github", Path(tmpdir), no_cache=True)
            self.assertFalse(result.found)
            self.assertIsNotNone(result.error)

    @patch("apm_cli.policy.discovery._fetch_github_contents")
    def test_invalid_policy_yaml(self, mock_fetch):
        mock_fetch.return_value = ("enforcement: bogus\n", None)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_repo("contoso/.github", Path(tmpdir), no_cache=True)
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
            result = _fetch_from_url("https://example.com/policy.yml", Path(tmpdir), no_cache=True)
            self.assertTrue(result.found)
            self.assertEqual(result.source, "url:https://example.com/policy.yml")

    @patch("apm_cli.policy.discovery.requests")
    def test_404(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_requests.get.return_value = mock_resp
        mock_requests.exceptions = __import__("requests").exceptions

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_url("https://example.com/policy.yml", Path(tmpdir), no_cache=True)
            self.assertFalse(result.found)
            self.assertIn("404", result.error)

    @patch("apm_cli.policy.discovery.requests")
    def test_timeout(self, mock_requests):
        import requests as real_requests

        mock_requests.exceptions = real_requests.exceptions
        mock_requests.get.side_effect = real_requests.exceptions.Timeout()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_url("https://example.com/policy.yml", Path(tmpdir), no_cache=True)
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
            result = _fetch_from_url("https://example.com/policy.yml", Path(tmpdir), no_cache=True)
            self.assertFalse(result.found)
            self.assertIn("Invalid policy", result.error)


class TestDiscoverPolicy(unittest.TestCase):
    """Integration-level tests for discover_policy."""

    def test_override_local_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "override-policy.yml"
            p.write_text(VALID_POLICY_YAML, encoding="utf-8")
            result = discover_policy(Path("/fake"), policy_override=str(p))
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
            result = discover_policy(Path(tmpdir), policy_override="org", no_cache=True)
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
            self.assertEqual(result.source, "org:contoso/.github-private")

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
        # .github-private will 404, falling through to .github which has a cache hit
        mock_fetch.return_value = (None, "404: Not Found")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Pre-populate cache for .github
            _write_cache("contoso/.github", _make_test_policy(), root)

            result = discover_policy(root, no_cache=False)
            self.assertTrue(result.found)
            self.assertTrue(result.cached)
            # .github-private is fetched (no cache), .github is served from cache
            self.assertEqual(mock_fetch.call_count, 1)

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
            self.assertEqual(result.source, "org:ghe.example.com/contoso/.github-private")


class TestAutoDiscover(unittest.TestCase):
    """Test _auto_discover logic with cascading candidate repos."""

    @patch("apm_cli.policy.discovery._fetch_from_repo")
    @patch("apm_cli.policy.discovery._extract_org_from_git_remote")
    def test_github_com_first_candidate_found(self, mock_extract, mock_fetch):
        """When .github-private has a policy, it wins immediately."""
        mock_extract.return_value = ("contoso", "github.com")
        mock_fetch.return_value = PolicyFetchResult(
            policy=ApmPolicy(), source="org:contoso/.github-private", outcome="found"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            # First call should be for .github-private
            first_call = mock_fetch.call_args_list[0]
            self.assertEqual(first_call[0][0], "contoso/.github-private")
            self.assertTrue(result.found)

    @patch("apm_cli.policy.discovery._fetch_from_repo")
    @patch("apm_cli.policy.discovery._extract_org_from_git_remote")
    def test_github_com_cascades_to_dot_apm(self, mock_extract, mock_fetch):
        """.github-private and .github absent -> falls back to .apm."""
        mock_extract.return_value = ("contoso", "github.com")
        mock_fetch.side_effect = [
            PolicyFetchResult(outcome="absent"),  # .github-private 404
            PolicyFetchResult(outcome="absent"),  # .github 404
            PolicyFetchResult(
                policy=ApmPolicy(), source="org:contoso/.apm", outcome="found"
            ),  # .apm found
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            self.assertEqual(mock_fetch.call_count, 3)
            self.assertEqual(mock_fetch.call_args_list[0][0][0], "contoso/.github-private")
            self.assertEqual(mock_fetch.call_args_list[1][0][0], "contoso/.github")
            self.assertEqual(mock_fetch.call_args_list[2][0][0], "contoso/.apm")
            self.assertTrue(result.found)

    @patch("apm_cli.policy.discovery._fetch_from_repo")
    @patch("apm_cli.policy.discovery._extract_org_from_git_remote")
    def test_github_com_cascades_to_underscore_apm(self, mock_extract, mock_fetch):
        """All dot-prefixed repos absent -> falls back to _apm."""
        mock_extract.return_value = ("contoso", "github.com")
        mock_fetch.side_effect = [
            PolicyFetchResult(outcome="absent"),  # .github-private 404
            PolicyFetchResult(outcome="absent"),  # .github 404
            PolicyFetchResult(outcome="absent"),  # .apm 404
            PolicyFetchResult(
                policy=ApmPolicy(), source="org:contoso/_apm", outcome="found"
            ),  # _apm found
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            self.assertEqual(mock_fetch.call_count, 4)
            self.assertTrue(result.found)

    @patch("apm_cli.policy.discovery._fetch_from_repo")
    @patch("apm_cli.policy.discovery._extract_org_from_git_remote")
    def test_github_com_all_absent(self, mock_extract, mock_fetch):
        """All candidates return absent -> outcome is absent."""
        mock_extract.return_value = ("contoso", "github.com")
        mock_fetch.return_value = PolicyFetchResult(outcome="absent")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            self.assertEqual(mock_fetch.call_count, 4)
            self.assertEqual(result.outcome, "absent")
            self.assertFalse(result.found)

    @patch("apm_cli.policy.discovery._fetch_from_repo")
    @patch("apm_cli.policy.discovery._extract_org_from_git_remote")
    def test_github_com_error_fail_closed(self, mock_extract, mock_fetch):
        """Auth error on first candidate -> fail-closed, no fallback."""
        mock_extract.return_value = ("contoso", "github.com")
        mock_fetch.return_value = PolicyFetchResult(
            error="401: Unauthorized", outcome="cache_miss_fetch_fail"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            # Only one call -- error stops the cascade
            self.assertEqual(mock_fetch.call_count, 1)
            self.assertFalse(result.found)
            self.assertIn("401", result.error)

    @patch("apm_cli.policy.discovery._fetch_from_repo")
    @patch("apm_cli.policy.discovery._extract_org_from_git_remote")
    def test_ghe_repo_ref_includes_host(self, mock_extract, mock_fetch):
        mock_extract.return_value = ("contoso", "ghe.example.com")
        mock_fetch.return_value = PolicyFetchResult(
            policy=ApmPolicy(),
            source="org:ghe.example.com/contoso/.github-private",
            outcome="found",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            _auto_discover(Path(tmpdir), no_cache=True)
            first_call = mock_fetch.call_args_list[0]
            self.assertEqual(first_call[0][0], "ghe.example.com/contoso/.github-private")

    @patch("apm_cli.policy.discovery._extract_org_from_git_remote")
    def test_no_remote_returns_error(self, mock_extract):
        mock_extract.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            self.assertFalse(result.found)
            self.assertIn("Could not determine org", result.error)

    @patch("apm_cli.policy.discovery._fetch_from_ado_repo")
    @patch("apm_cli.policy.discovery._extract_org_from_git_remote")
    def test_ado_host_only_tries_underscore_apm(self, mock_extract, mock_ado_fetch):
        """ADO host profile skips .github and .apm, only tries _apm."""
        mock_extract.return_value = ("contoso", "dev.azure.com")
        mock_ado_fetch.return_value = PolicyFetchResult(
            policy=ApmPolicy(), source="org:dev.azure.com/contoso/_apm/_apm", outcome="found"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            mock_ado_fetch.assert_called_once()
            call_kwargs = mock_ado_fetch.call_args
            self.assertEqual(call_kwargs[1]["repo"], "_apm")
            self.assertEqual(call_kwargs[1]["project"], "_apm")
            self.assertTrue(result.found)

    @patch("apm_cli.policy.discovery._fetch_from_ado_repo")
    @patch("apm_cli.policy.discovery._extract_org_from_git_remote")
    def test_ado_visualstudio_host(self, mock_extract, mock_ado_fetch):
        """*.visualstudio.com hosts also use ADO profile."""
        mock_extract.return_value = ("contoso", "contoso.visualstudio.com")
        mock_ado_fetch.return_value = PolicyFetchResult(outcome="absent")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            mock_ado_fetch.assert_called_once()
            self.assertEqual(result.outcome, "absent")


class TestPolicyRepoCandidates(unittest.TestCase):
    """Test _policy_repo_candidates host profile selection."""

    def test_github_com_returns_all_candidates(self):
        result = _policy_repo_candidates("github.com")
        self.assertEqual(result, (".github-private", ".github", ".apm", "_apm"))

    def test_ghe_returns_all_candidates(self):
        result = _policy_repo_candidates("ghe.example.com")
        self.assertEqual(result, (".github-private", ".github", ".apm", "_apm"))

    def test_ado_dev_azure_com(self):
        result = _policy_repo_candidates("dev.azure.com")
        self.assertEqual(result, ("_apm",))

    def test_ado_ssh_dev_azure_com(self):
        result = _policy_repo_candidates("ssh.dev.azure.com")
        self.assertEqual(result, ("_apm",))

    def test_ado_visualstudio_com(self):
        result = _policy_repo_candidates("contoso.visualstudio.com")
        self.assertEqual(result, ("_apm",))

    def test_unknown_host_returns_all(self):
        result = _policy_repo_candidates("gitlab.example.com")
        self.assertEqual(result, (".github-private", ".github", ".apm", "_apm"))


class TestFetchAdoContents(unittest.TestCase):
    """Test _fetch_ado_contents for Azure DevOps Items API."""

    def _auth_context(self, token: str | None, scheme: str = "basic"):
        ctx = MagicMock()
        ctx.token = token
        ctx.auth_scheme = scheme
        return ctx

    def _resolver(self, mock_resolver_cls, token: str | None, scheme: str = "basic"):
        resolver = mock_resolver_cls.return_value
        resolver.resolve.return_value = self._auth_context(token, scheme)
        resolver.build_error_context.return_value = "\n    auth remediation"
        return resolver

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.requests.get")
    def test_success(self, mock_get, mock_resolver_cls):
        resolver = self._resolver(mock_resolver_cls, "my-ado-pat")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = VALID_POLICY_YAML
        mock_get.return_value = mock_resp

        content, error = _fetch_ado_contents("contoso", "_apm", "_apm", "apm-policy.yml")
        self.assertIsNone(error)
        self.assertEqual(content, VALID_POLICY_YAML)
        # Verify Basic auth header was sent with ADO_APM_PAT
        call_kwargs = mock_get.call_args
        headers = call_kwargs[1].get("headers", {})
        self.assertIn("Basic", headers.get("Authorization", ""))
        resolver.resolve.assert_called_once_with("dev.azure.com", org="contoso")

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.requests.get")
    def test_404_returns_error(self, mock_get, mock_resolver_cls):
        self._resolver(mock_resolver_cls, "my-ado-pat")
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        content, error = _fetch_ado_contents("contoso", "_apm", "_apm", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("404", error)

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.requests.get")
    def test_401_returns_error(self, mock_get, mock_resolver_cls):
        resolver = self._resolver(mock_resolver_cls, "my-ado-pat")
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_get.return_value = mock_resp

        content, error = _fetch_ado_contents("contoso", "_apm", "_apm", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("401", error)
        self.assertIn("auth remediation", error)
        resolver.build_error_context.assert_called_once_with(
            "dev.azure.com", "fetch org policy", org="contoso"
        )

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.requests.get")
    def test_redirect_rejected(self, mock_get, mock_resolver_cls):
        self._resolver(mock_resolver_cls, "my-ado-pat")
        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {"Location": "https://evil.example.com"}
        mock_get.return_value = mock_resp

        content, error = _fetch_ado_contents("contoso", "_apm", "_apm", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("redirect", error.lower())

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.requests.get")
    def test_no_auth_token_still_sends_request(self, mock_get, mock_resolver_cls):
        """Unauthenticated requests are allowed (public ADO repos)."""
        self._resolver(mock_resolver_cls, None)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = VALID_POLICY_YAML
        mock_get.return_value = mock_resp

        _content, error = _fetch_ado_contents("contoso", "_apm", "_apm", "apm-policy.yml")
        self.assertIsNone(error)
        # Verify no Authorization header was sent
        call_kwargs = mock_get.call_args
        headers = call_kwargs[1].get("headers", {})
        self.assertNotIn("Authorization", headers)

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.requests.get")
    def test_authresolver_bearer_token_uses_bearer_header(self, mock_get, mock_resolver_cls):
        """ADO bearer tokens from AuthResolver use Bearer auth."""
        self._resolver(mock_resolver_cls, "fallback-token", scheme="bearer")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = VALID_POLICY_YAML
        mock_get.return_value = mock_resp

        _content, error = _fetch_ado_contents("contoso", "_apm", "_apm", "apm-policy.yml")
        self.assertIsNone(error)
        call_kwargs = mock_get.call_args
        headers = call_kwargs[1].get("headers", {})
        self.assertEqual(headers.get("Authorization"), "Bearer fallback-token")


class TestFetchFromAdoRepo(unittest.TestCase):
    """Test _fetch_from_ado_repo orchestration around the ADO transport."""

    @patch("apm_cli.policy.discovery._fetch_ado_contents")
    def test_200_caches_result(self, mock_fetch):
        mock_fetch.return_value = (VALID_POLICY_YAML, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = _fetch_from_ado_repo(
                org="contoso",
                project="_apm",
                repo="_apm",
                host="dev.azure.com",
                project_root=root,
                no_cache=True,
            )
            self.assertTrue(result.found)
            self.assertEqual(result.source, "org:dev.azure.com/contoso/_apm/_apm")
            self.assertFalse(result.cached)

    @patch("apm_cli.policy.discovery._fetch_ado_contents")
    def test_404_no_error(self, mock_fetch):
        mock_fetch.return_value = (None, "404: Policy file not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_ado_repo(
                org="contoso",
                project="_apm",
                repo="_apm",
                host="dev.azure.com",
                project_root=Path(tmpdir),
                no_cache=True,
            )
            self.assertFalse(result.found)
            self.assertEqual(result.outcome, "absent")
            self.assertIsNone(result.error)

    @patch("apm_cli.policy.discovery._fetch_ado_contents")
    def test_api_error_uses_stale_cache(self, mock_fetch):
        mock_fetch.return_value = (None, "Connection error fetching policy")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "dev.azure.com/contoso/_apm/_apm"
            _write_cache(repo_ref, _make_test_policy(), root)
            cache_dir = _get_cache_dir(root)
            key = _cache_key(repo_ref)
            meta_file = cache_dir / f"{key}.meta.json"
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            meta["cached_at"] = time.time() - DEFAULT_CACHE_TTL - 100
            meta_file.write_text(json.dumps(meta), encoding="utf-8")

            result = _fetch_from_ado_repo(
                org="contoso",
                project="_apm",
                repo="_apm",
                host="dev.azure.com",
                project_root=root,
            )
            self.assertTrue(result.found)
            self.assertTrue(result.cached)
            self.assertEqual(result.outcome, "cached_stale")

    @patch("apm_cli.policy.discovery._fetch_ado_contents")
    def test_invalid_policy_yaml(self, mock_fetch):
        mock_fetch.return_value = ("enforcement: bogus\n", None)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_ado_repo(
                org="contoso",
                project="_apm",
                repo="_apm",
                host="dev.azure.com",
                project_root=Path(tmpdir),
                no_cache=True,
            )
            self.assertFalse(result.found)
            self.assertIn("Invalid policy", result.error)

    @patch("apm_cli.policy.discovery._fetch_ado_contents")
    def test_hash_pin_mismatch(self, mock_fetch):
        mock_fetch.return_value = (VALID_POLICY_YAML, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_ado_repo(
                org="contoso",
                project="_apm",
                repo="_apm",
                host="dev.azure.com",
                project_root=Path(tmpdir),
                no_cache=True,
                expected_hash="sha256:" + ("0" * 64),
            )
            self.assertFalse(result.found)
            self.assertEqual(result.outcome, "hash_mismatch")


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
