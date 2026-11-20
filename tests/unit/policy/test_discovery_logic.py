"""Tests for apm_cli.policy.discovery  --  policy auto-discovery engine (pure logic cases)."""

from __future__ import annotations

import base64
import os
import types
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, quote, urlparse, urlsplit

import pytest

from apm_cli.core.auth import AuthResolver as _RealAuthResolver
from apm_cli.policy._gitlab import (
    _fetch_gitlab_contents,
    _gitlab_namespace_descending,
)
from apm_cli.policy.discovery import (
    MAX_STALE_TTL,  # noqa: F401
    PolicyFetchResult,
    _extract_extends_host,
    _fetch_ado_contents,
    _fetch_github_contents,
    _parse_remote_url,
    _policy_repo_candidates,
    _remote_url_parts,
    _validate_extends_host,
)
from apm_cli.policy.parser import PolicyValidationError, load_policy  # noqa: F401
from apm_cli.policy.schema import ApmPolicy
from tests.utils.simulated_ado_chain import StaticPolicyResolver

pytestmark = pytest.mark.unit

# Minimal valid YAML that produces a valid ApmPolicy
VALID_POLICY_YAML = "name: test-policy\nversion: '1.0'\nenforcement: warn\n"


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

    # --- Nested GitLab subgroups: org stays the top-level group (#2753) ---

    def test_https_gitlab_nested_subgroups_org_is_top_level(self):
        result = _parse_remote_url("https://gitlab.com/acme/dept-a/team-x/my-project.git")
        self.assertEqual(result, ("acme", "gitlab.com"))

    def test_ssh_gitlab_nested_subgroups_org_is_top_level(self):
        result = _parse_remote_url("git@gitlab.com:acme/dept-a/team-x/my-project.git")
        self.assertEqual(result, ("acme", "gitlab.com"))


class TestRemoteUrlParts(unittest.TestCase):
    """Test _remote_url_parts -- the shared host + path-segments splitter."""

    def test_https_nested_segments(self):
        result = _remote_url_parts("https://gitlab.com/acme/dept-a/team-x/my-project.git")
        self.assertEqual(result, ("gitlab.com", ["acme", "dept-a", "team-x", "my-project"]))

    def test_ssh_nested_segments(self):
        result = _remote_url_parts("git@gitlab.com:acme/dept-a/team-x/my-project.git")
        self.assertEqual(result, ("gitlab.com", ["acme", "dept-a", "team-x", "my-project"]))

    def test_https_flat_segments(self):
        result = _remote_url_parts("https://gitlab.com/acme/my-project")
        self.assertEqual(result, ("gitlab.com", ["acme", "my-project"]))

    def test_https_trailing_slash_is_cleaned(self):
        result = _remote_url_parts("https://gitlab.com/acme/my-project/")
        self.assertEqual(result, ("gitlab.com", ["acme", "my-project"]))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_remote_url_parts(""))

    def test_non_url_returns_none(self):
        self.assertIsNone(_remote_url_parts("not-a-url"))

    def test_ssh_empty_path_returns_none(self):
        self.assertIsNone(_remote_url_parts("git@gitlab.com:"))

    def test_https_no_path_returns_none(self):
        self.assertIsNone(_remote_url_parts("https://gitlab.com/"))


class TestGitlabNamespaceDescending(unittest.TestCase):
    """Test _gitlab_namespace_descending -- subgroup probe order (#2753).

    The helper is pure: it takes the already-read ``origin`` URL so discovery
    reads the remote exactly once.
    """

    def test_nested_subgroups_deepest_first(self):
        result = _gitlab_namespace_descending(
            "https://gitlab.com/acme/dept-a/team-x/my-project.git"
        )
        self.assertEqual(result, ["acme/dept-a/team-x", "acme/dept-a", "acme"])

    def test_flat_project_yields_single_top_level(self):
        result = _gitlab_namespace_descending("git@gitlab.com:acme/my-project.git")
        self.assertEqual(result, ["acme"])

    def test_no_remote_returns_none(self):
        self.assertIsNone(_gitlab_namespace_descending(None))

    def test_no_namespace_segment_returns_none(self):
        # A remote with only one path segment has no owning namespace.
        self.assertIsNone(_gitlab_namespace_descending("https://gitlab.com/solo"))


class TestFetchGithubContents(unittest.TestCase):
    """Test _fetch_github_contents with mocked requests."""

    def _b64_response(self, content: str) -> dict:
        """Create a GitHub API response with base64-encoded content."""
        return {
            "encoding": "base64",
            "content": base64.b64encode(content.encode()).decode(),
        }

    @patch(
        "apm_cli.policy.discovery._policy_auth_resolver", return_value=StaticPolicyResolver(None)
    )
    @patch("apm_cli.policy.discovery.policy_get")
    def test_200_base64_content(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = self._b64_response(VALID_POLICY_YAML)
        mock_requests.return_value = mock_resp

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(error)
        self.assertEqual(content, VALID_POLICY_YAML)

    @patch(
        "apm_cli.policy.discovery._policy_auth_resolver", return_value=StaticPolicyResolver(None)
    )
    @patch("apm_cli.policy.discovery.policy_get")
    def test_200_plain_content(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"content": VALID_POLICY_YAML}
        mock_requests.return_value = mock_resp

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(error)
        self.assertEqual(content, VALID_POLICY_YAML)

    @patch(
        "apm_cli.policy.discovery._policy_auth_resolver", return_value=StaticPolicyResolver(None)
    )
    @patch("apm_cli.policy.discovery.policy_get")
    def test_404(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_requests.return_value = mock_resp

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("404", error)

    @patch(
        "apm_cli.policy.discovery._policy_auth_resolver", return_value=StaticPolicyResolver(None)
    )
    @patch("apm_cli.policy.discovery.policy_get")
    def test_403(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_requests.return_value = mock_resp

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("403", error)

    @patch(
        "apm_cli.policy.discovery._policy_auth_resolver", return_value=StaticPolicyResolver(None)
    )
    @patch("apm_cli.policy.discovery.policy_get")
    def test_timeout(self, mock_requests, _mock_token):
        import requests as real_requests

        mock_requests.side_effect = real_requests.exceptions.Timeout()

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("Timeout", error)

    @patch(
        "apm_cli.policy.discovery._policy_auth_resolver", return_value=StaticPolicyResolver(None)
    )
    @patch("apm_cli.policy.discovery.policy_get")
    def test_connection_error(self, mock_requests, _mock_token):
        import requests as real_requests

        mock_requests.side_effect = real_requests.exceptions.ConnectionError()

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("Connection error", error)

    @patch(
        "apm_cli.policy.discovery._policy_auth_resolver", return_value=StaticPolicyResolver(None)
    )
    @patch("apm_cli.policy.discovery.policy_get")
    def test_unexpected_response_format(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"type": "dir"}
        mock_requests.return_value = mock_resp

        content, error = _fetch_github_contents("contoso/.github", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("Unexpected response", error)

    @patch(
        "apm_cli.policy.discovery._policy_auth_resolver", return_value=StaticPolicyResolver(None)
    )
    @patch("apm_cli.policy.discovery.policy_get")
    def test_invalid_repo_ref(self, mock_requests, _mock_token):
        content, error = _fetch_github_contents("invalid", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("Invalid repo reference", error)

    @patch(
        "apm_cli.policy.discovery._policy_auth_resolver",
        return_value=StaticPolicyResolver("ghp_test123"),
    )
    @patch("apm_cli.policy.discovery.policy_get")
    def test_auth_header_sent(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = self._b64_response(VALID_POLICY_YAML)
        mock_requests.return_value = mock_resp

        _fetch_github_contents("contoso/.github", "apm-policy.yml")

        call_kwargs = mock_requests.call_args[1]
        self.assertIn("Authorization", call_kwargs["headers"])
        self.assertEqual(call_kwargs["headers"]["Authorization"], "token ghp_test123")

    @patch(
        "apm_cli.policy.discovery._policy_auth_resolver", return_value=StaticPolicyResolver(None)
    )
    @patch("apm_cli.policy.discovery.policy_get")
    def test_ghe_api_url(self, mock_requests, _mock_token):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_requests.return_value = mock_resp

        _fetch_github_contents("ghe.example.com/contoso/.github", "apm-policy.yml")

        call_url = mock_requests.call_args[0][0]
        self.assertTrue(call_url.startswith("https://ghe.example.com/api/v3/repos/"))


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


class TestValidateExtendsHostNestedNamespace(unittest.TestCase):
    """The pre-fetch host-pin guard must allow nested GitLab namespaces (#2753).

    ``_validate_extends_host`` runs BEFORE the GitLab adapter fetch, so a bare
    nested-namespace ``extends:`` ref (single-label first segment) must not be
    misread as a cross-host reference -- while a real attacker FQDN still is.
    """

    def test_nested_namespace_ref_is_not_a_host_on_gitlab_leaf(self):
        # First segment is a top-level group, not a host -> shorthand (GitLab).
        self.assertIsNone(_extract_extends_host("acme/dept-a/apm-policy", "gitlab.com"))

    def test_single_label_first_segment_stays_a_host_on_github_leaf(self):
        # F1: on a non-GitLab leaf a single-label first segment is a host, so a
        # cross-host ``extends: "evil/org/repo"`` must NOT be treated as
        # same-host (which would route a credential to ``evil``).
        self.assertEqual(_extract_extends_host("evil/org/.github", "github.com"), "evil")

    def test_host_qualified_nested_ref_extracts_host(self):
        self.assertEqual(
            _extract_extends_host("gitlab.com/acme/dept-a/apm-policy", "gitlab.com"),
            "gitlab.com",
        )

    def test_validate_allows_nested_same_host_reference(self):
        # Must NOT raise: this is the exact ref the subgroup feature enables.
        _validate_extends_host("gitlab.com", "acme/dept-a/apm-policy")

    def test_validate_rejects_single_label_cross_host_on_github_leaf(self):
        import apm_cli.policy.inheritance as _inh

        with self.assertRaisesRegex(_inh.PolicyInheritanceError, "cross-host"):
            _validate_extends_host("github.com", "evil/org/.github")

    def test_validate_still_rejects_cross_host_fqdn(self):
        import apm_cli.policy.inheritance as _inh

        with self.assertRaisesRegex(_inh.PolicyInheritanceError, "cross-host"):
            _validate_extends_host("gitlab.com", "evil.example.com/org/apm-policy")


class TestFetchAdoContents(unittest.TestCase):
    """Test _fetch_ado_contents for Azure DevOps Items API."""

    def _auth_context(self, token: str | None, scheme: str = "basic"):
        ctx = MagicMock()
        ctx.token = token
        ctx.auth_scheme = scheme
        ctx.git_env = (
            _RealAuthResolver._build_git_env(token, scheme=scheme, host_kind="ado", base_env={})
            if token
            else {}
        )
        return ctx

    @staticmethod
    def _sent_authorization(call) -> str | None:
        request = types.SimpleNamespace(headers=dict(call.kwargs.get("headers") or {}))
        auth = call.kwargs.get("auth")
        if callable(auth):
            auth(request)
        return request.headers.get("Authorization")

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
    @patch("apm_cli.policy.discovery.policy_get")
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
        self.assertIn("Basic", self._sent_authorization(mock_get.call_args) or "")
        resolver.resolve.assert_called_once_with("dev.azure.com", org="contoso")

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.policy_get")
    def test_404_returns_error(self, mock_get, mock_resolver_cls):
        self._resolver(mock_resolver_cls, "my-ado-pat")
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        content, error = _fetch_ado_contents("contoso", "apm", "apm-policy", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("404", error)

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.policy_get")
    def test_401_returns_error(self, mock_get, mock_resolver_cls):
        from apm_cli.core.auth_chain import AdoAuthChainExhaustedError, AuthAttemptLog

        resolver = self._resolver(mock_resolver_cls, "my-ado-pat")
        attempts = AuthAttemptLog()
        resolver.try_with_fallback.side_effect = AdoAuthChainExhaustedError(
            "Authentication failed for dev.azure.com. Tried in order: ADO_APM_PAT was rejected",
            attempts=attempts,
        )

        content, error = _fetch_ado_contents("contoso", "apm", "apm-policy", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("ADO_APM_PAT was rejected", error)
        self.assertIn("auth remediation", error)
        resolver.build_error_context.assert_called_once_with(
            "dev.azure.com", "fetch org policy", org="contoso", attempts=attempts
        )
        mock_get.assert_not_called()

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.policy_get")
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
    @patch("apm_cli.policy.discovery.policy_get")
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
        self.assertIsNone(self._sent_authorization(mock_get.call_args))

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.policy_get")
    def test_authresolver_bearer_token_uses_bearer_header(self, mock_get, mock_resolver_cls):
        """ADO bearer tokens from AuthResolver use Bearer auth."""
        self._resolver(mock_resolver_cls, "fallback-token", scheme="bearer")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = VALID_POLICY_YAML
        mock_get.return_value = mock_resp

        _content, error = _fetch_ado_contents("contoso", "apm", "apm-policy", "apm-policy.yml")
        self.assertIsNone(error)
        self.assertEqual(self._sent_authorization(mock_get.call_args), "Bearer fallback-token")

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy.discovery.policy_get")
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
    @patch("apm_cli.policy.discovery.policy_get")
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
                        "GIT_CONFIG_KEY_0": "http.extraheader",
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
        scheme, credential = self._sent_authorization(mock_get.call_args_list[1]).split(" ", 1)
        self.assertEqual(scheme, "Bearer")
        self.assertEqual(credential, "fresh-bearer")


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
    @patch("apm_cli.policy._gitlab.policy_get")
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
            patch("apm_cli.policy._gitlab.policy_get", side_effect=fake_get) as mock_get,
        ):
            content, error = _fetch_gitlab_contents("contoso", "apm-policy", "apm-policy.yml")

        self.assertIsNone(error)
        self.assertEqual(content, VALID_POLICY_YAML)
        # The token must be on the FIRST request -- not a retry after an
        # unauthenticated attempt was silently accepted as "not found".
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(mock_get.call_args.kwargs["headers"].get("PRIVATE-TOKEN"), "glpat_secret")

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy._gitlab.policy_get")
    def test_404_returns_error(self, mock_get, mock_resolver_cls):
        self._resolver(mock_resolver_cls, "my-gitlab-token")
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        content, error = _fetch_gitlab_contents("contoso", "apm-policy", "apm-policy.yml")
        self.assertIsNone(content)
        self.assertIn("404", error)

    @patch("apm_cli.core.auth.AuthResolver")
    @patch("apm_cli.policy._gitlab.policy_get")
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
    @patch("apm_cli.policy._gitlab.policy_get")
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
    @patch("apm_cli.policy._gitlab.policy_get")
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
    @patch("apm_cli.policy._gitlab.policy_get")
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
    @patch("apm_cli.policy._gitlab.policy_get")
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
    @patch("apm_cli.policy._gitlab.policy_get")
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
