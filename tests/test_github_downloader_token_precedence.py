"""Additional tests for GitHubPackageDownloader token precedence."""

import os
from unittest.mock import patch

import pytest  # noqa: F401

from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.utils import github_host


class TestGitHubDownloaderTokenPrecedence:
    """Test token precedence in GitHubPackageDownloader."""

    def test_apm_pat_precedence_over_github_token(self):
        """Test that GITHUB_APM_PAT takes precedence over GITHUB_TOKEN for APM module access."""
        with patch.dict(
            os.environ,
            {"GITHUB_APM_PAT": "apm-specific-token", "GITHUB_TOKEN": "generic-token"},
            clear=True,
        ):
            downloader = GitHubPackageDownloader()

            # Should use GITHUB_APM_PAT for github_token property (modules purpose)
            assert downloader.github_token == "apm-specific-token"
            assert downloader.has_github_token is True

            # Environment should preserve existing GITHUB_TOKEN (GitHubTokenManager preserves existing)
            env = downloader.git_env
            assert env["GITHUB_TOKEN"] == "generic-token"  # Original GITHUB_TOKEN preserved
            # GH_TOKEN should also use GITHUB_TOKEN since it was already set (preserve_existing=True)
            assert env["GH_TOKEN"] == "generic-token"  # Preserves existing GITHUB_TOKEN

    def test_github_token_fallback_when_no_apm_pat(self):
        """Test fallback to GITHUB_TOKEN when GITHUB_APM_PAT is not available."""
        with patch.dict(os.environ, {"GITHUB_TOKEN": "fallback-token"}, clear=True):
            # Ensure GITHUB_APM_PAT is not set
            if "GITHUB_APM_PAT" in os.environ:
                del os.environ["GITHUB_APM_PAT"]

            downloader = GitHubPackageDownloader()

            # Should use GITHUB_TOKEN as fallback
            assert downloader.github_token == "fallback-token"
            assert downloader.has_github_token is True

            # Environment should be set up correctly
            env = downloader.git_env
            assert env["GH_TOKEN"] == "fallback-token"

    def test_no_tokens_available(self):
        """Test behavior when no GitHub tokens are available."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None),
        ):
            downloader = GitHubPackageDownloader()

            # Should have no token
            assert downloader.github_token is None
            assert downloader.has_github_token is False

    def test_public_repo_access_without_token(self):
        """Test that public repos can be accessed without tokens."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None),
        ):
            downloader = GitHubPackageDownloader()

            # Should work for public repos
            assert downloader.has_github_token is False

            # Build URL should work for public repos
            public_url = downloader._build_repo_url("octocat/Hello-World", use_ssh=False)
            expected_public = f"https://{github_host.default_host()}/octocat/Hello-World"
            assert public_url == expected_public

    def test_private_repo_url_building_with_token(self):
        """Test URL building for private repos with authentication."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "private-repo-token"}, clear=True):
            downloader = GitHubPackageDownloader()

            # Should build authenticated URL for private repos
            auth_url = downloader._build_repo_url("private-org/private-repo", use_ssh=False)
            expected_url = github_host.build_https_clone_url(
                github_host.default_host(), "private-org/private-repo", token="private-repo-token"
            )
            assert auth_url == expected_url

    def test_ssh_url_building(self):
        """Test SSH URL building regardless of token availability."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "some-token"}, clear=True):
            downloader = GitHubPackageDownloader()

            # Should build SSH URL when requested
            ssh_url = downloader._build_repo_url("user/repo", use_ssh=True)
            expected_ssh = github_host.build_ssh_url(github_host.default_host(), "user/repo")
            assert ssh_url == expected_ssh

    def test_error_message_sanitization_with_new_token(self):
        """Test that error messages properly sanitize the new token names."""
        downloader = GitHubPackageDownloader()

        # Test sanitization of GITHUB_APM_PAT
        error_with_token = "Error: GITHUB_APM_PAT=ghp_secrettoken123 failed"
        sanitized = downloader._sanitize_git_error(error_with_token)
        assert "ghp_secrettoken123" not in sanitized
        assert "GITHUB_APM_PAT=***" in sanitized

        # Test sanitization of URLs with tokens
        host = github_host.default_host()
        error_with_url = (
            f"fatal: Authentication failed for 'https://ghp_secrettoken123@{host}/user/repo.git'"
        )
        sanitized = downloader._sanitize_git_error(error_with_url)
        assert "ghp_secrettoken123" not in sanitized
        assert f"https://***@{host}" in sanitized


class TestGitHubDownloaderErrorMessages:
    """Test error messages for authentication failures."""

    def test_authentication_error_message_references_correct_tokens(self):
        """Test that authentication error messages reference the correct token names."""
        downloader = GitHubPackageDownloader()

        # Simulate an authentication failure message
        error_msg = downloader._sanitize_git_error(
            "Authentication failed. For private repositories, set GITHUB_APM_PAT or GITHUB_TOKEN environment variable"
        )

        # Should mention the correct token names
        assert "GITHUB_APM_PAT" in error_msg
        assert "GITHUB_TOKEN" in error_msg
        # Should not mention the old token name
        assert "GITHUB_CLI_PAT" not in error_msg

    def test_ado_token_sanitization_cloud(self):
        """Test that ADO Cloud URLs with tokens are sanitized."""
        downloader = GitHubPackageDownloader()

        # ADO Cloud URL with token
        error_with_ado = "fatal: Authentication failed for 'https://ado_secret_pat@dev.azure.com/myorg/myproject/_git/myrepo'"
        sanitized = downloader._sanitize_git_error(error_with_ado)
        assert "ado_secret_pat" not in sanitized
        assert "https://***@dev.azure.com" in sanitized

    def test_ado_token_sanitization_custom_server(self):
        """Test that ADO Server (on-prem) URLs with custom domains are sanitized."""
        downloader = GitHubPackageDownloader()

        # ADO Server with custom domain
        error_with_ado_onprem = "fatal: Authentication failed for 'https://my_ado_token@ado.company.internal/DefaultCollection/Project/_git/repo'"
        sanitized = downloader._sanitize_git_error(error_with_ado_onprem)
        assert "my_ado_token" not in sanitized
        assert "https://***@ado.company.internal" in sanitized

    def test_ado_token_sanitization_tfs_server(self):
        """Test that TFS/Azure DevOps Server URLs are sanitized."""
        downloader = GitHubPackageDownloader()

        # TFS-style URL
        error_with_tfs = "fatal: could not read from 'https://secret123@tfs.corp.net:8080/tfs/DefaultCollection/_git/myrepo'"
        sanitized = downloader._sanitize_git_error(error_with_tfs)
        assert "secret123" not in sanitized
        assert "https://***@tfs.corp.net:8080" in sanitized

    def test_ado_pat_env_var_sanitization(self):
        """Test that ADO_APM_PAT environment variable values are sanitized."""
        downloader = GitHubPackageDownloader()

        # Error containing ADO_APM_PAT value
        error_with_pat = "Error: ADO_APM_PAT=my_secret_ado_token failed to authenticate"
        sanitized = downloader._sanitize_git_error(error_with_pat)
        assert "my_secret_ado_token" not in sanitized
        assert "ADO_APM_PAT=***" in sanitized
