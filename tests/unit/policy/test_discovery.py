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
from unittest.mock import ANY, MagicMock, patch
from urllib.parse import parse_qs, quote, urlparse, urlsplit

from apm_cli.core.auth import AuthResolver as _RealAuthResolver
from apm_cli.policy._gitlab import (
    _fetch_from_gitlab_repo,
    _fetch_gitlab_contents,
    _gitlab_project_state_via_git,
)
from apm_cli.policy.discovery import (
    CACHE_SCHEMA_VERSION,  # noqa: F401
    DEFAULT_CACHE_TTL,
    MAX_STALE_TTL,  # noqa: F401
    PolicyFetchResult,
    _auto_discover,
    _cache_key,
    _extract_org_from_git_remote,
    _extract_org_host_port_from_git_remote,
    _fetch_ado_contents,
    _fetch_ado_org_policy,
    _fetch_chain_parent,
    _fetch_from_ado_repo,
    _fetch_from_repo,
    _fetch_from_url,
    _fetch_github_contents,
    _get_cache_dir,
    _load_from_file,
    _parse_remote_url,
    _policy_repo_candidates,
    _read_cache,
    _read_cache_entry,
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

    def test_https_gitlab(self):
        result = _parse_remote_url("https://gitlab.com/contoso/my-project.git")
        self.assertEqual(result, ("contoso", "gitlab.com"))

    def test_ssh_gitlab_self_managed(self):
        result = _parse_remote_url("git@gitlab.example.com:contoso/my-project.git")
        self.assertEqual(result, ("contoso", "gitlab.example.com"))

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

    def test_ado_server_tfs_base_path_is_rejected(self):
        with patch.dict(os.environ, {"ADO_HOST": "ado.example.test"}, clear=False):
            with self.assertRaisesRegex(ValueError, "mounted below '/tfs/'"):
                _parse_remote_url(
                    "https://ado.example.test/tfs/DefaultCollection/project/_git/repo"
                )

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

    @patch("apm_cli.policy.discovery._parse_remote_url")
    @patch("apm_cli.policy.discovery.subprocess.run")
    def test_remote_parser_value_error_returns_none(self, mock_run, mock_parse):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://dev.azure.com/contoso/project/_git/repo\n",
        )
        mock_parse.side_effect = ValueError("invalid remote coordinates")

        result = _extract_org_host_port_from_git_remote(Path("/fake"))

        self.assertIsNone(result)

    @patch("apm_cli.policy.discovery.urlparse")
    @patch("apm_cli.policy.discovery._parse_remote_url")
    @patch("apm_cli.policy.discovery.subprocess.run")
    def test_remote_port_value_error_returns_none(self, mock_run, mock_parse, mock_urlparse):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://ghe.example.com:invalid/contoso/repo.git\n",
        )
        mock_parse.return_value = ("contoso", "ghe.example.com")
        mock_urlparse.side_effect = ValueError("invalid port")

        result = _extract_org_host_port_from_git_remote(Path("/fake"))

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

    def test_policy_warnings_survive_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "contoso/.github"
            warnings = ["Unknown top-level policy key: 'enforcment'"]

            _write_cache(repo_ref, _make_test_policy(), root, warnings=warnings)

            result = _read_cache(repo_ref, root)
            self.assertIsNotNone(result)
            self.assertEqual(result.warnings, warnings)

    def test_corrupt_cached_warnings_render_gracefully(self):
        cases = (
            ("not-a-list", [], "none"),
            (["unknown key", 7, None], ["unknown key", "7", "None"], "unknown key; 7; None"),
        )

        for corrupt_warnings, expected_warnings, expected_rendering in cases:
            with self.subTest(warnings=corrupt_warnings):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    repo_ref = "contoso/.github"
                    _write_cache(repo_ref, _make_test_policy(), root)

                    meta_file = _get_cache_dir(root) / f"{_cache_key(repo_ref)}.meta.json"
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    meta["warnings"] = corrupt_warnings
                    meta_file.write_text(json.dumps(meta), encoding="utf-8")

                    result = _read_cache(repo_ref, root)

                    self.assertIsNotNone(result)
                    self.assertEqual(result.warnings, expected_warnings)
                    rendered = "; ".join(result.warnings) if result.warnings else "none"
                    self.assertEqual(rendered, expected_rendering)

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
    def test_extending_leaf_waits_for_completed_chain_before_cache(self, mock_fetch):
        mock_fetch.return_value = (
            "name: child\nextends: parent/.github\n",
            None,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "contoso/.github"
            result = _fetch_from_repo(repo_ref, root, no_cache=True)
            self.assertIsNotNone(result.policy)
            self.assertEqual(result.policy.extends, "parent/.github")
            self.assertIsNone(_read_cache_entry(repo_ref, root))

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
        mock_fetch.return_value = (None, "404: Policy file not found")

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
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_github_com_first_candidate_found(self, mock_extract, mock_fetch):
        """When .github-private has a policy, it wins immediately."""
        mock_extract.return_value = ("contoso", "github.com", None)
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
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_github_com_cascades_to_dot_apm(self, mock_extract, mock_fetch):
        """.github-private and .github absent -> falls back to .apm."""
        mock_extract.return_value = ("contoso", "github.com", None)
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
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_github_com_cascades_to_underscore_apm(self, mock_extract, mock_fetch):
        """All dot-prefixed repos absent -> falls back to _apm."""
        mock_extract.return_value = ("contoso", "github.com", None)
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
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_github_com_all_absent(self, mock_extract, mock_fetch):
        """All candidates return absent -> outcome is absent."""
        mock_extract.return_value = ("contoso", "github.com", None)
        mock_fetch.return_value = PolicyFetchResult(outcome="absent")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            self.assertEqual(mock_fetch.call_count, 4)
            self.assertEqual(result.outcome, "absent")
            self.assertFalse(result.found)

    @patch("apm_cli.policy.discovery._fetch_from_repo")
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_github_com_error_fail_closed(self, mock_extract, mock_fetch):
        """Auth error on first candidate -> fail-closed, no fallback."""
        mock_extract.return_value = ("contoso", "github.com", None)
        mock_fetch.return_value = PolicyFetchResult(
            error="401: Unauthorized", outcome="cache_miss_fetch_fail"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            # Only one call -- error stops the cascade
            self.assertEqual(mock_fetch.call_count, 1)
            self.assertEqual(mock_fetch.call_args_list[0][0][0], "contoso/.github-private")
            self.assertFalse(result.found)
            self.assertIn("401", result.error)

    @patch("apm_cli.policy.discovery._fetch_from_repo")
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_403_is_error_not_absent(self, mock_extract, mock_fetch):
        """HTTP 403 -> fail-closed (cache_miss_fetch_fail), not absent."""
        mock_extract.return_value = ("contoso", "github.com", None)
        mock_fetch.return_value = PolicyFetchResult(
            error="403: Access denied to contoso/.github-private",
            outcome="cache_miss_fetch_fail",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            self.assertEqual(mock_fetch.call_count, 1)
            self.assertEqual(result.outcome, "cache_miss_fetch_fail")
            self.assertNotEqual(result.outcome, "absent")
            self.assertFalse(result.found)

    @patch("apm_cli.policy.discovery._fetch_from_repo")
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_github_private_auth_error_does_not_fall_through(self, mock_extract, mock_fetch):
        """.github-private auth error -> fail-closed, .github NOT tried."""
        mock_extract.return_value = ("contoso", "github.com", None)
        mock_fetch.side_effect = [
            PolicyFetchResult(error="403: Access denied", outcome="cache_miss_fetch_fail"),
            PolicyFetchResult(policy=ApmPolicy(), outcome="found"),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            # Only .github-private tried -- error stops cascade
            self.assertEqual(mock_fetch.call_count, 1)
            self.assertFalse(result.found)

    @patch("apm_cli.policy.discovery._fetch_from_repo")
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_github_private_absent_falls_back_to_github(self, mock_extract, mock_fetch):
        """.github-private absent (404) -> cascade to .github."""
        mock_extract.return_value = ("contoso", "github.com", None)
        mock_fetch.side_effect = [
            PolicyFetchResult(outcome="absent"),  # .github-private 404
            PolicyFetchResult(
                policy=ApmPolicy(), source="org:contoso/.github", outcome="found"
            ),  # .github found
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            self.assertEqual(mock_fetch.call_count, 2)
            self.assertEqual(mock_fetch.call_args_list[0][0][0], "contoso/.github-private")
            self.assertEqual(mock_fetch.call_args_list[1][0][0], "contoso/.github")
            self.assertTrue(result.found)

    @patch("apm_cli.policy.discovery._fetch_from_repo")
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_ghe_repo_ref_includes_host(self, mock_extract, mock_fetch):
        mock_extract.return_value = ("contoso", "ghe.example.com", None)
        mock_fetch.return_value = PolicyFetchResult(
            policy=ApmPolicy(),
            source="org:ghe.example.com/contoso/.github-private",
            outcome="found",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            _auto_discover(Path(tmpdir), no_cache=True)
            first_call = mock_fetch.call_args_list[0]
            self.assertEqual(first_call[0][0], "ghe.example.com/contoso/.github-private")

    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_no_remote_returns_error(self, mock_extract):
        mock_extract.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            self.assertFalse(result.found)
            self.assertIn("Could not determine org", result.error)

    @patch("apm_cli.policy.discovery._fetch_from_ado_repo")
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_ado_host_only_tries_apm_policy(self, mock_extract, mock_ado_fetch):
        """ADO host profile skips .github and .apm, only tries apm-policy."""
        mock_extract.return_value = ("contoso", "dev.azure.com", None)
        mock_ado_fetch.return_value = PolicyFetchResult(
            policy=ApmPolicy(), source="org:dev.azure.com/contoso/apm/apm-policy", outcome="found"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            mock_ado_fetch.assert_called_once()
            call_kwargs = mock_ado_fetch.call_args
            self.assertEqual(call_kwargs[1]["repo"], "apm-policy")
            self.assertEqual(call_kwargs[1]["project"], "apm")
            self.assertTrue(result.found)

    @patch("apm_cli.policy.discovery._fetch_from_ado_repo")
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_ado_server_auto_discovery_preserves_remote_port(
        self,
        mock_extract,
        mock_ado_fetch,
    ):
        mock_extract.return_value = (
            "DefaultCollection",
            "ado.example.test",
            8443,
        )
        mock_ado_fetch.return_value = PolicyFetchResult(outcome="absent")

        with (
            patch.dict(
                os.environ,
                {"ADO_HOST": "ado.example.test"},
                clear=False,
            ),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            _auto_discover(Path(tmpdir), no_cache=True)

        self.assertEqual(mock_ado_fetch.call_args.kwargs["port"], 8443)

    @patch("apm_cli.policy.discovery._fetch_from_ado_repo")
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_ado_visualstudio_host(self, mock_extract, mock_ado_fetch):
        """*.visualstudio.com hosts also use ADO profile."""
        mock_extract.return_value = ("contoso", "contoso.visualstudio.com", None)
        mock_ado_fetch.return_value = PolicyFetchResult(outcome="absent")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            mock_ado_fetch.assert_called_once()
            self.assertEqual(result.outcome, "absent")

    @patch("apm_cli.policy.discovery._fetch_from_ado_repo")
    @patch("apm_cli.policy.discovery._policy_repo_candidates")
    def test_auto_discover_ado_git_subprocess_invoked_exactly_once(
        self,
        mock_candidates,
        mock_ado_fetch,
    ):
        """_auto_discover must invoke the git remote subprocess exactly once.

        Before the A1 fix, separate org/host and port callers in _auto_discover
        each ran git remote. This guard asserts one subprocess call and verifies
        that the parsed host and explicit port reach their routing consumers.
        """
        ado_url = "https://ado.example.test:8443/DefaultCollection/project/_git/repo"

        def fake_run(cmd, **kwargs):
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ado_url + "\n"
            return proc

        mock_ado_fetch.return_value = PolicyFetchResult(outcome="absent")
        mock_candidates.return_value = ("_apm",)

        with (
            patch("apm_cli.policy.discovery.subprocess.run", side_effect=fake_run) as mock_run,
            patch.dict(os.environ, {"ADO_HOST": "ado.example.test"}, clear=False),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            _auto_discover(Path(tmpdir), no_cache=True)

        self.assertEqual(
            mock_run.call_count,
            1,
            f"Expected exactly 1 git subprocess call, got {mock_run.call_count}. "
            "_auto_discover must not invoke git remote multiple times.",
        )
        mock_candidates.assert_called_once_with("ado.example.test")
        self.assertEqual(mock_ado_fetch.call_args.kwargs["port"], 8443)

    @patch("apm_cli.policy._gitlab._fetch_from_gitlab_repo")
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_gitlab_host_only_tries_apm_policy(self, mock_extract, mock_gitlab_fetch):
        """GitLab host profile skips the GitHub-family cascade, only tries apm-policy."""
        mock_extract.return_value = ("contoso", "gitlab.com", None)
        mock_gitlab_fetch.return_value = PolicyFetchResult(
            policy=ApmPolicy(), source="org:gitlab.com/contoso/apm-policy", outcome="found"
        )

        with (
            patch.dict(os.environ, {"APM_GITLAB_POLICY_REPO": ""}, clear=False),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            result = _auto_discover(Path(tmpdir), no_cache=True)
            mock_gitlab_fetch.assert_called_once()
            call_kwargs = mock_gitlab_fetch.call_args.kwargs
            self.assertEqual(call_kwargs["repo"], "apm-policy")
            self.assertEqual(call_kwargs["org"], "contoso")
            self.assertTrue(result.found)

    @patch("apm_cli.policy._gitlab._fetch_from_gitlab_repo")
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_gitlab_self_managed_preserves_remote_port(self, mock_extract, mock_gitlab_fetch):
        mock_extract.return_value = ("contoso", "gitlab.example.test", 8443)
        mock_gitlab_fetch.return_value = PolicyFetchResult(outcome="absent")

        with (
            patch.dict(os.environ, {"GITLAB_HOST": "gitlab.example.test"}, clear=False),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            result = _auto_discover(Path(tmpdir), no_cache=True)

        self.assertEqual(mock_gitlab_fetch.call_args.kwargs["port"], 8443)
        # Absent (no policy published) must be a clean no-op, not an error.
        self.assertEqual(result.outcome, "absent")
        self.assertIsNone(result.error)

    @patch("apm_cli.policy._gitlab._fetch_from_gitlab_repo")
    @patch("apm_cli.policy.discovery._extract_org_host_port_from_git_remote")
    def test_gitlab_absent_is_clean_no_op(self, mock_extract, mock_gitlab_fetch):
        """No apm-policy project on GitLab -> absent, not a fetch-failure warning."""
        mock_extract.return_value = ("contoso", "gitlab.com", None)
        mock_gitlab_fetch.return_value = PolicyFetchResult(outcome="absent")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _auto_discover(Path(tmpdir), no_cache=True)
            self.assertFalse(result.found)
            self.assertEqual(result.outcome, "absent")
            self.assertIsNone(result.error)


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
        self.assertEqual(result, ("apm-policy",))

    def test_ado_ssh_dev_azure_com(self):
        result = _policy_repo_candidates("ssh.dev.azure.com")
        self.assertEqual(result, ("apm-policy",))

    def test_ado_visualstudio_com(self):
        result = _policy_repo_candidates("contoso.visualstudio.com")
        self.assertEqual(result, ("apm-policy",))

    def test_unknown_host_returns_all(self):
        result = _policy_repo_candidates("gitlab.example.com")
        self.assertEqual(result, (".github-private", ".github", ".apm", "_apm"))

    def test_gitlab_com_only_tries_apm_policy(self):
        """GitLab rejects a leading '.' or '_' in project paths (#2566)."""
        with patch.dict(os.environ, {"APM_GITLAB_POLICY_REPO": ""}, clear=False):
            result = _policy_repo_candidates("gitlab.com")
        self.assertEqual(result, ("apm-policy",))

    def test_gitlab_self_managed_via_env_only_tries_apm_policy(self):
        with patch.dict(
            os.environ,
            {"GITLAB_HOST": "gitlab.example.com", "APM_GITLAB_POLICY_REPO": ""},
            clear=False,
        ):
            result = _policy_repo_candidates("gitlab.example.com")
        self.assertEqual(result, ("apm-policy",))

    def test_gitlab_policy_repo_env_override(self):
        with patch.dict(os.environ, {"APM_GITLAB_POLICY_REPO": "org-policy"}, clear=False):
            result = _policy_repo_candidates("gitlab.com")
        self.assertEqual(result, ("org-policy",))

    def test_gitlab_policy_repo_rejects_path_like_override(self):
        with patch.dict(os.environ, {"APM_GITLAB_POLICY_REPO": "other/project"}, clear=False):
            with self.assertRaisesRegex(ValueError, "one GitLab project-name segment"):
                _policy_repo_candidates("gitlab.com")

    def test_gitlab_policy_repo_rejects_leading_underscore(self):
        with patch.dict(os.environ, {"APM_GITLAB_POLICY_REPO": "_apm"}, clear=False):
            with self.assertRaisesRegex(ValueError, "no leading"):
                _policy_repo_candidates("gitlab.com")


class TestGitlabPolicyInheritance(unittest.TestCase):
    """GitLab policy parents must remain within the private GitLab adapter."""

    @patch("apm_cli.policy._gitlab._fetch_from_gitlab_repo")
    def test_gitlab_parent_uses_gitlab_adapter(self, mock_fetch):
        mock_fetch.return_value = PolicyFetchResult(outcome="absent")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_chain_parent(
                "platform/baseline",
                current_source="org:gitlab.com/contoso/apm-policy",
                leaf_host="gitlab.com",
                project_root=Path(tmpdir),
                no_cache=True,
            )

        self.assertEqual(result.outcome, "absent")
        mock_fetch.assert_called_once()
        self.assertEqual(mock_fetch.call_args.kwargs["org"], "platform")
        self.assertEqual(mock_fetch.call_args.kwargs["repo"], "baseline")
        self.assertEqual(mock_fetch.call_args.kwargs["host"], "gitlab.com")

    @patch("apm_cli.policy._gitlab._fetch_from_gitlab_repo")
    def test_gitlab_parent_preserves_leaf_port(self, mock_fetch):
        """GitLab inheritance keeps the leaf server port in the adapter."""
        mock_fetch.return_value = PolicyFetchResult(outcome="absent")

        with (
            patch.dict(os.environ, {"GITLAB_HOST": "gitlab.example.test"}, clear=False),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            _fetch_chain_parent(
                "platform/baseline",
                current_source="org:gitlab.example.test:8443/contoso/apm-policy",
                leaf_host="gitlab.example.test",
                leaf_port=8443,
                project_root=Path(tmpdir),
                no_cache=True,
            )

        self.assertEqual(mock_fetch.call_args.kwargs["host"], "gitlab.example.test")
        self.assertEqual(mock_fetch.call_args.kwargs["port"], 8443)

    @patch("apm_cli.policy._gitlab._fetch_from_gitlab_repo")
    def test_gitlab_parent_accepts_same_host_qualified_reference(self, mock_fetch):
        """A qualified parent on the pinned GitLab host stays adapter-local."""
        mock_fetch.return_value = PolicyFetchResult(outcome="absent")

        with tempfile.TemporaryDirectory() as tmpdir:
            _fetch_chain_parent(
                "gitlab.com/platform/baseline",
                current_source="org:gitlab.com/contoso/apm-policy",
                leaf_host="gitlab.com",
                leaf_port=None,
                project_root=Path(tmpdir),
                no_cache=True,
            )

        self.assertEqual(mock_fetch.call_args.kwargs["org"], "platform")
        self.assertEqual(mock_fetch.call_args.kwargs["repo"], "baseline")


class TestFetchAdoContents(unittest.TestCase):
    """Test _fetch_ado_contents for Azure DevOps Items API."""

    def _auth_context(self, token: str | None, scheme: str = "basic"):
        ctx = MagicMock()
        ctx.token = token
        ctx.auth_scheme = scheme
        ctx.git_env = {}
        if scheme == "bearer" and token:
            ctx.git_env = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
            }
        return ctx

    def _resolver(self, mock_resolver_cls, token: str | None, scheme: str = "basic"):
        resolver = mock_resolver_cls.return_value
        resolver.resolve.return_value = self._auth_context(token, scheme)
        resolver.build_error_context.return_value = "\n    auth remediation"

        def try_with_fallback(host, operation, **kwargs):
            resolve_kwargs = {"org": kwargs.get("org")}
            if kwargs.get("port") is not None:
                resolve_kwargs["port"] = kwargs["port"]
            ctx = resolver.resolve(host, **resolve_kwargs)
            return operation(ctx.token, ctx.git_env)

        resolver.try_with_fallback.side_effect = try_with_fallback
        return resolver

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.requests.get")
    def test_success(self, mock_get, mock_resolver_cls):
        resolver = self._resolver(mock_resolver_cls, "my-ado-pat")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = VALID_POLICY_YAML
        mock_get.return_value = mock_resp

        content, error = _fetch_ado_contents("contoso", "apm", "apm-policy", "apm-policy.yml")
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

        content, error = _fetch_ado_contents("contoso", "apm", "apm-policy", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("404", error)

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.requests.get")
    def test_401_returns_error(self, mock_get, mock_resolver_cls):
        resolver = self._resolver(mock_resolver_cls, "my-ado-pat")
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_get.return_value = mock_resp

        content, error = _fetch_ado_contents("contoso", "apm", "apm-policy", "apm-policy.yml")
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

        content, error = _fetch_ado_contents("contoso", "apm", "apm-policy", "apm-policy.yml")
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

        _content, error = _fetch_ado_contents("contoso", "apm", "apm-policy", "apm-policy.yml")
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

        _content, error = _fetch_ado_contents("contoso", "apm", "apm-policy", "apm-policy.yml")
        self.assertIsNone(error)
        call_kwargs = mock_get.call_args
        headers = call_kwargs[1].get("headers", {})
        self.assertEqual(headers.get("Authorization"), "Bearer fallback-token")

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.requests.get")
    def test_custom_server_port_reaches_policy_api(
        self,
        mock_get,
        mock_resolver_cls,
    ):
        resolver = self._resolver(mock_resolver_cls, "my-ado-pat")
        mock_get.return_value = MagicMock(
            status_code=200,
            text=VALID_POLICY_YAML,
        )

        with patch.dict(os.environ, {"ADO_HOST": "ado.example.test"}, clear=False):
            content, error = _fetch_ado_contents(
                "DefaultCollection",
                "apm",
                "apm",
                "apm-policy.yml",
                host="ado.example.test",
                port=8443,
            )

        self.assertIsNone(error)
        self.assertEqual(content, VALID_POLICY_YAML)
        parsed = urlparse(mock_get.call_args.args[0])
        self.assertEqual(parsed.hostname, "ado.example.test")
        self.assertEqual(parsed.port, 8443)
        resolver.resolve.assert_called_once_with(
            "ado.example.test",
            org="DefaultCollection",
            port=8443,
        )

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.requests.get")
    def test_rejected_services_pat_retries_policy_with_bearer(
        self,
        mock_get,
        mock_resolver_cls,
    ):
        resolver = self._resolver(mock_resolver_cls, "stale-pat")
        mock_get.side_effect = [
            MagicMock(status_code=401),
            MagicMock(status_code=200, text=VALID_POLICY_YAML),
        ]

        def fallback(_host, operation, **_kwargs):
            try:
                operation("stale-pat", {})
            except RuntimeError:
                return operation(
                    "fresh-bearer",
                    {
                        "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_VALUE_0": ("Authorization: Bearer fresh-bearer"),
                    },
                )
            raise AssertionError("stale PAT unexpectedly succeeded")

        resolver.try_with_fallback.side_effect = fallback
        content, error = _fetch_ado_contents(
            "contoso",
            "apm",
            "apm",
            "apm-policy.yml",
        )

        self.assertIsNone(error)
        self.assertEqual(content, VALID_POLICY_YAML)
        self.assertEqual(mock_get.call_count, 2)
        scheme, credential = (
            mock_get.call_args_list[1].kwargs["headers"]["Authorization"].split(" ", 1)
        )
        self.assertEqual(scheme, "Bearer")
        self.assertEqual(credential, "fresh-bearer")


class TestFetchFromAdoRepo(unittest.TestCase):
    """Test _fetch_from_ado_repo orchestration around the ADO transport."""

    @patch("apm_cli.policy.discovery._fetch_from_ado_repo")
    def test_legacy_coordinate_is_used_only_after_primary_404(self, mock_fetch):
        """The migration fallback must be bounded to a primary 404."""
        mock_fetch.side_effect = [
            PolicyFetchResult(outcome="absent", not_found=True),
            PolicyFetchResult(
                policy=_make_test_policy(),
                source="org:dev.azure.com/contoso/_apm/_apm",
                outcome="found",
            ),
        ]

        result = _fetch_ado_org_policy(
            org="contoso",
            host="dev.azure.com",
            project_root=Path("."),
            no_cache=True,
        )

        self.assertTrue(result.found)
        self.assertEqual(mock_fetch.call_count, 2)
        self.assertEqual(mock_fetch.call_args_list[0].kwargs["repo"], "apm-policy")
        self.assertEqual(mock_fetch.call_args_list[1].kwargs["project"], "_apm")
        self.assertEqual(mock_fetch.call_args_list[1].kwargs["repo"], "_apm")
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("Move it to apm/apm-policy", result.warnings[0])

    @patch("apm_cli.policy.discovery._fetch_from_ado_repo")
    def test_legacy_coordinate_is_not_used_after_non_404_failure(self, mock_fetch):
        """Auth, network, and malformed failures must never probe legacy policy."""
        mock_fetch.return_value = PolicyFetchResult(
            outcome="cache_miss_fetch_fail",
            error="403: Access denied",
        )

        result = _fetch_ado_org_policy(
            org="contoso",
            host="dev.azure.com",
            project_root=Path("."),
            no_cache=True,
        )

        self.assertEqual(result.outcome, "cache_miss_fetch_fail")
        mock_fetch.assert_called_once()

    @patch("apm_cli.policy.discovery._fetch_ado_contents")
    def test_200_caches_result(self, mock_fetch):
        mock_fetch.return_value = (VALID_POLICY_YAML, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = _fetch_from_ado_repo(
                org="contoso",
                project="apm",
                repo="apm-policy",
                host="dev.azure.com",
                project_root=root,
                no_cache=True,
            )
            self.assertTrue(result.found)
            self.assertEqual(result.source, "org:dev.azure.com/contoso/apm/apm-policy")
            self.assertFalse(result.cached)

    @patch("apm_cli.policy.discovery._fetch_ado_contents")
    def test_extending_leaf_waits_for_completed_chain_before_cache(
        self, mock_fetch: MagicMock
    ) -> None:
        mock_fetch.return_value = (
            "name: child\nextends: parent/.github\n",
            None,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "dev.azure.com/contoso/apm/apm-policy"
            result = _fetch_from_ado_repo(
                org="contoso",
                project="apm",
                repo="apm-policy",
                host="dev.azure.com",
                project_root=root,
                no_cache=True,
            )
            self.assertIsNotNone(result.policy)
            self.assertEqual(result.policy.extends, "parent/.github")
            self.assertIsNone(_read_cache_entry(repo_ref, root))

    @patch("apm_cli.policy.discovery._fetch_ado_contents")
    def test_404_no_error(self, mock_fetch):
        mock_fetch.return_value = (None, "404: Policy file not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_ado_repo(
                org="contoso",
                project="apm",
                repo="apm-policy",
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
            repo_ref = "dev.azure.com/contoso/apm/apm-policy"
            _write_cache(repo_ref, _make_test_policy(), root)
            cache_dir = _get_cache_dir(root)
            key = _cache_key(repo_ref)
            meta_file = cache_dir / f"{key}.meta.json"
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            meta["cached_at"] = time.time() - DEFAULT_CACHE_TTL - 100
            meta_file.write_text(json.dumps(meta), encoding="utf-8")

            result = _fetch_from_ado_repo(
                org="contoso",
                project="apm",
                repo="apm-policy",
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
                project="apm",
                repo="apm-policy",
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
                project="apm",
                repo="apm-policy",
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


class TestFetchGitlabContents(unittest.TestCase):
    """Test _fetch_gitlab_contents for the GitLab Repository Files API."""

    def _auth_context(self, token: str | None):
        ctx = MagicMock()
        ctx.token = token
        ctx.git_env = {}
        return ctx

    def _resolver(self, mock_resolver_cls, token: str | None):
        # gitlab_rest_headers is a plain @staticmethod (no instance state), so
        # delegate the mocked class's attribute to the real implementation --
        # otherwise ``AuthResolver.gitlab_rest_headers(token)`` in production
        # code would return a MagicMock instead of real headers. _RealAuthResolver
        # was imported at module load time, before @patch replaced the name in
        # apm_cli.core.auth, so it still points at the genuine class.
        mock_resolver_cls.gitlab_rest_headers = _RealAuthResolver.gitlab_rest_headers

        resolver = mock_resolver_cls.return_value
        resolver.resolve.return_value = self._auth_context(token)
        resolver.build_error_context.return_value = "\n    auth remediation"

        def try_with_fallback(host, operation, **kwargs):
            resolve_kwargs = {"org": kwargs.get("org")}
            if kwargs.get("port") is not None:
                resolve_kwargs["port"] = kwargs["port"]
            ctx = resolver.resolve(host, **resolve_kwargs)
            return operation(ctx.token, ctx.git_env)

        resolver.try_with_fallback.side_effect = try_with_fallback
        return resolver

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy._gitlab.requests.get")
    def test_success(self, mock_get, mock_resolver_cls):
        self._resolver(mock_resolver_cls, "my-gitlab-token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = VALID_POLICY_YAML
        mock_get.return_value = mock_resp

        content, error = _fetch_gitlab_contents("contoso", "apm-policy", "apm-policy.yml")
        self.assertIsNone(error)
        self.assertEqual(content, VALID_POLICY_YAML)
        call_kwargs = mock_get.call_args
        headers = call_kwargs[1].get("headers", {})
        self.assertEqual(headers.get("PRIVATE-TOKEN"), "my-gitlab-token")
        api_url = mock_get.call_args.args[0]
        parsed = urlsplit(api_url)
        self.assertEqual(
            parsed.path,
            "/api/v4/projects/"
            + quote("contoso/apm-policy", safe="")
            + "/repository/files/apm-policy.yml/raw",
        )
        self.assertEqual(parse_qs(parsed.query).get("ref"), ["HEAD"])

    def test_private_project_authenticates_on_first_attempt(self):
        """Regression guard (Copilot review on PR #2605): auth-first, not unauth-first.

        GitLab returns 404 (not 401/403) to an unauthenticated request for a
        private project, to avoid leaking its existence. This fetcher's
        ``_request`` only raises on 401/403, so ``unauth_first=True`` would
        let ``try_with_fallback`` accept that unauthenticated 404 as "the"
        result and never retry with a configured token -- permanently
        masking a private policy repo even with a valid GITLAB_APM_PAT set.
        Exercises the REAL AuthResolver (not the simplified single-call stub
        used by the other tests in this class) so the actual try_with_fallback
        branching between an unauth-first and an auth-first attempt is
        under test.
        """
        from apm_cli.core.token_manager import GitHubTokenManager

        def fake_get(_url, headers=None, **_kwargs):
            resp = MagicMock()
            if headers and headers.get("PRIVATE-TOKEN") == "glpat_secret":
                resp.status_code = 200
                resp.text = VALID_POLICY_YAML
            else:
                resp.status_code = 404
            return resp

        env = {"GITLAB_APM_PAT": "glpat_secret"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None),
            patch("apm_cli.policy._gitlab.requests.get", side_effect=fake_get) as mock_get,
        ):
            content, error = _fetch_gitlab_contents("contoso", "apm-policy", "apm-policy.yml")

        self.assertIsNone(error)
        self.assertEqual(content, VALID_POLICY_YAML)
        # The token must be on the FIRST request -- not a retry after an
        # unauthenticated attempt was silently accepted as "not found".
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(mock_get.call_args.kwargs["headers"].get("PRIVATE-TOKEN"), "glpat_secret")

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy._gitlab.requests.get")
    def test_404_returns_error(self, mock_get, mock_resolver_cls):
        self._resolver(mock_resolver_cls, "my-gitlab-token")
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        content, error = _fetch_gitlab_contents("contoso", "apm-policy", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("404", error)

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy._gitlab.requests.get")
    def test_410_returns_not_found_error(self, mock_get, mock_resolver_cls):
        """Self-managed GitLab has been observed returning 410 for a missing project (#2566)."""
        self._resolver(mock_resolver_cls, "my-gitlab-token")
        mock_resp = MagicMock()
        mock_resp.status_code = 410
        mock_get.return_value = mock_resp

        content, error = _fetch_gitlab_contents("contoso", "apm-policy", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("410", error)

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy._gitlab.requests.get")
    def test_401_returns_error(self, mock_get, mock_resolver_cls):
        resolver = self._resolver(mock_resolver_cls, "my-gitlab-token")
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_get.return_value = mock_resp

        content, error = _fetch_gitlab_contents("contoso", "apm-policy", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("401", error)
        self.assertIn("auth remediation", error)
        resolver.build_error_context.assert_called_once_with(
            "gitlab.com", "fetch org policy", org="contoso"
        )

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy._gitlab.requests.get")
    def test_request_exception_redacts_header_value(self, mock_get, mock_resolver_cls):
        """Request validation errors must not render credential-bearing headers."""
        from requests.exceptions import InvalidHeader

        self._resolver(mock_resolver_cls, "glpat_secret")
        mock_get.side_effect = InvalidHeader("Invalid header PRIVATE-TOKEN: glpat_secret")

        content, error = _fetch_gitlab_contents("contoso", "apm-policy", "apm-policy.yml")

        self.assertIsNone(content)
        self.assertEqual(error, "Request error fetching policy from gitlab.com/contoso/apm-policy")
        self.assertNotIn("glpat_secret", error)

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy._gitlab.requests.get")
    def test_redirect_rejected(self, mock_get, mock_resolver_cls):
        self._resolver(mock_resolver_cls, "my-gitlab-token")
        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {"Location": "https://token:secret@evil.example.com/?token=secret"}
        mock_get.return_value = mock_resp

        content, error = _fetch_gitlab_contents("contoso", "apm-policy", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("redirect", error.lower())
        self.assertNotIn("secret", error)

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy._gitlab.requests.get")
    def test_no_auth_token_still_sends_request(self, mock_get, mock_resolver_cls):
        """Unauthenticated requests are allowed (public GitLab projects)."""
        self._resolver(mock_resolver_cls, None)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = VALID_POLICY_YAML
        mock_get.return_value = mock_resp

        _content, error = _fetch_gitlab_contents("contoso", "apm-policy", "apm-policy.yml")
        self.assertIsNone(error)
        call_kwargs = mock_get.call_args
        headers = call_kwargs[1].get("headers", {})
        self.assertNotIn("PRIVATE-TOKEN", headers)
        self.assertNotIn("Authorization", headers)

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy._gitlab.requests.get")
    def test_custom_self_managed_host_and_port(self, mock_get, mock_resolver_cls):
        resolver = self._resolver(mock_resolver_cls, "my-gitlab-token")
        mock_get.return_value = MagicMock(status_code=200, text=VALID_POLICY_YAML)

        with patch.dict(os.environ, {"GITLAB_HOST": "gitlab.example.test"}, clear=False):
            content, error = _fetch_gitlab_contents(
                "contoso",
                "apm-policy",
                "apm-policy.yml",
                host="gitlab.example.test",
                port=8443,
            )

        self.assertIsNone(error)
        self.assertEqual(content, VALID_POLICY_YAML)
        parsed = urlparse(mock_get.call_args.args[0])
        self.assertEqual(parsed.hostname, "gitlab.example.test")
        self.assertEqual(parsed.port, 8443)
        resolver.resolve.assert_called_once_with(
            "gitlab.example.test",
            org="contoso",
            port=8443,
        )


class TestFetchFromGitlabRepo(unittest.TestCase):
    """Test _fetch_from_gitlab_repo orchestration around the GitLab transport."""

    @patch("apm_cli.policy._gitlab._fetch_gitlab_contents")
    def test_200_caches_result(self, mock_fetch):
        mock_fetch.return_value = (VALID_POLICY_YAML, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = _fetch_from_gitlab_repo(
                org="contoso",
                repo="apm-policy",
                host="gitlab.com",
                project_root=root,
                no_cache=True,
            )
            self.assertTrue(result.found)
            self.assertEqual(result.source, "org:gitlab.com/contoso/apm-policy")
            self.assertFalse(result.cached)

    @patch("apm_cli.policy._gitlab._fetch_gitlab_contents")
    def test_404_no_error(self, mock_fetch):
        mock_fetch.return_value = (None, "gitlab-status:404: Policy file not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = _fetch_from_gitlab_repo(
                org="contoso",
                repo="apm-policy",
                host="gitlab.com",
                project_root=root,
                no_cache=True,
            )
            self.assertFalse(result.found)
            self.assertEqual(result.outcome, "absent")
            self.assertIsNone(result.error)
            self.assertFalse((root / ".apm").exists())

    @patch("apm_cli.policy._gitlab._gitlab_project_state_via_git", return_value=None)
    @patch("apm_cli.policy._gitlab._fetch_gitlab_contents")
    def test_ambiguous_410_fails_closed_when_project_state_is_unavailable(
        self, mock_fetch, mock_project_state
    ):
        """A 410 never turns into absence when Git cannot prove reachability."""
        mock_fetch.return_value = (None, "gitlab-status:410: Policy file not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = _fetch_from_gitlab_repo(
                org="contoso",
                repo="apm-policy",
                host="gitlab.example.test",
                project_root=root,
                no_cache=True,
            )
            self.assertFalse(result.found)
            self.assertEqual(result.outcome, "cache_miss_fetch_fail")
            self.assertIsNotNone(result.error)
            self.assertFalse((root / ".apm").exists())
        mock_project_state.assert_called_once()

    @patch("apm_cli.policy._gitlab._gitlab_project_state_via_git", return_value=True)
    @patch("apm_cli.policy._gitlab._fetch_gitlab_contents")
    def test_api_disabled_410_fails_closed(self, mock_fetch, mock_project_state):
        """A reachable project with a 410 Files API must not skip policy."""
        mock_fetch.return_value = (None, "gitlab-status:410: Policy file not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = _fetch_from_gitlab_repo(
                org="contoso",
                repo="apm-policy",
                host="gitlab.example.test",
                project_root=root,
                no_cache=True,
            )

        self.assertEqual(result.outcome, "cache_miss_fetch_fail")
        self.assertIsNotNone(result.error)
        self.assertFalse((root / ".apm").exists())
        mock_project_state.assert_called_once()

    @patch("apm_cli.policy._gitlab.subprocess.run")
    @patch("apm_cli.core.auth.AuthResolver")
    def test_git_reachability_probe_uses_bounded_auth_resolver_credentials(
        self, mock_resolver_cls, mock_run
    ):
        resolver = mock_resolver_cls.return_value
        result = MagicMock(returncode=0, stderr="")
        mock_run.return_value = result

        def try_with_fallback(host, operation, **kwargs):
            return operation(
                "glpat_secret",
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraheader",
                    "GIT_CONFIG_VALUE_0": "Authorization: Basic c2FmZQ==",
                    "GIT_TOKEN": "glpat_secret",
                },
            )

        resolver.try_with_fallback.side_effect = try_with_fallback

        state = _gitlab_project_state_via_git(
            org="contoso",
            repo="apm-policy",
            host="gitlab.example.test",
            port=8443,
        )

        self.assertTrue(state)
        mock_run.assert_called_once()
        command = mock_run.call_args.args[0]
        self.assertEqual(Path(command[0]).name, "git")
        self.assertEqual(
            command[1:],
            [
                "ls-remote",
                "--exit-code",
                "https://gitlab.example.test:8443/contoso/apm-policy.git",
                "HEAD",
            ],
        )
        run_kwargs = mock_run.call_args.kwargs
        self.assertEqual(run_kwargs["timeout"], 10)
        self.assertEqual(run_kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("GIT_TOKEN", run_kwargs["env"])
        header_values = [
            value for key, value in run_kwargs["env"].items() if key.startswith("GIT_CONFIG_VALUE_")
        ]
        self.assertTrue(any(value.startswith("Authorization: Basic ") for value in header_values))
        resolver.try_with_fallback.assert_called_once_with(
            "gitlab.example.test",
            ANY,
            org="contoso",
            port=8443,
            path="contoso/apm-policy",
            host_type="gitlab",
            unauth_first=False,
        )

    @patch("apm_cli.policy._gitlab.subprocess.run")
    @patch("apm_cli.core.auth.AuthResolver")
    def test_git_reachability_probe_fails_closed_for_concealed_project(
        self, mock_resolver_cls, mock_run
    ):
        resolver = mock_resolver_cls.return_value
        mock_run.return_value = MagicMock(returncode=128, stderr="remote: Project not found")
        resolver.try_with_fallback.side_effect = lambda host, operation, **kwargs: operation(
            "glpat_secret", {}
        )

        state = _gitlab_project_state_via_git(
            org="contoso",
            repo="apm-policy",
            host="gitlab.example.test",
            port=None,
        )

        self.assertIsNone(state)

    @patch("apm_cli.policy._gitlab._fetch_gitlab_contents")
    def test_auth_failure_fails_closed(self, mock_fetch):
        mock_fetch.return_value = (None, "401: unauthorized")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = _fetch_from_gitlab_repo(
                org="contoso",
                repo="apm-policy",
                host="gitlab.com",
                project_root=root,
                no_cache=True,
            )

        self.assertEqual(result.outcome, "cache_miss_fetch_fail")
        self.assertIsNotNone(result.error)
        self.assertFalse((root / ".apm").exists())

    @patch("apm_cli.policy._gitlab._fetch_gitlab_contents")
    def test_api_error_uses_stale_cache(self, mock_fetch):
        mock_fetch.return_value = (None, "Connection error fetching policy")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "gitlab.com/contoso/apm-policy"
            _write_cache(repo_ref, _make_test_policy(), root)
            cache_dir = _get_cache_dir(root)
            key = _cache_key(repo_ref)
            meta_file = cache_dir / f"{key}.meta.json"
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            meta["cached_at"] = time.time() - DEFAULT_CACHE_TTL - 100
            meta_file.write_text(json.dumps(meta), encoding="utf-8")

            result = _fetch_from_gitlab_repo(
                org="contoso",
                repo="apm-policy",
                host="gitlab.com",
                project_root=root,
            )
            self.assertTrue(result.found)
            self.assertTrue(result.cached)
            self.assertEqual(result.outcome, "cached_stale")

    @patch("apm_cli.policy._gitlab._fetch_gitlab_contents")
    def test_invalid_policy_yaml(self, mock_fetch):
        mock_fetch.return_value = ("enforcement: bogus\n", None)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_gitlab_repo(
                org="contoso",
                repo="apm-policy",
                host="gitlab.com",
                project_root=Path(tmpdir),
                no_cache=True,
            )
            self.assertFalse(result.found)
            self.assertIn("Invalid policy", result.error)


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
