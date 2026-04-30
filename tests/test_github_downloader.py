"""Tests for GitHub package downloader."""

import os
import shutil
import stat
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import urlparse

import pytest
import requests as requests_lib

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import (
    APMPackage,
    DependencyReference,
    GitReferenceType,
    ResolvedReference,
    ValidationResult,
)

_CRED_FILL_PATCH = patch(
    "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
    return_value=None,
)


class TestGitHubPackageDownloader:
    """Test cases for GitHubPackageDownloader."""

    def setup_method(self):
        """Set up test fixtures."""
        self.downloader = GitHubPackageDownloader()
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_setup_git_environment_with_github_apm_pat(self):
        """Test Git environment setup with GITHUB_APM_PAT."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "test-token"}, clear=True):
            downloader = GitHubPackageDownloader()
            env = downloader.git_env

            # GITHUB_APM_PAT should be used for github_token property (modules purpose)
            assert downloader.github_token == "test-token"
            assert downloader.has_github_token is True
            # But GITHUB_TOKEN should not be set in env since it wasn't there originally
            assert "GITHUB_TOKEN" not in env or env.get("GITHUB_TOKEN") == "test-token"
            assert env["GH_TOKEN"] == "test-token"

    def test_setup_git_environment_with_github_token(self):
        """Test Git environment setup with GITHUB_TOKEN fallback."""
        with patch.dict(os.environ, {"GITHUB_TOKEN": "fallback-token"}, clear=True):
            downloader = GitHubPackageDownloader()
            env = downloader.git_env

            assert env["GH_TOKEN"] == "fallback-token"

    def test_setup_git_environment_no_token(self):
        """Test Git environment setup with no GitHub token."""
        with patch.dict(os.environ, {}, clear=True):
            downloader = GitHubPackageDownloader()
            env = downloader.git_env

            # Should not have GitHub tokens in environment
            assert "GITHUB_TOKEN" not in env or not env["GITHUB_TOKEN"]
            assert "GH_TOKEN" not in env or not env["GH_TOKEN"]

    def test_setup_git_environment_does_not_eagerly_call_credential_helper(self):
        """Constructor should not invoke git credential helper (lazy per-dep auth)."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git"
            ) as mock_cred,
        ):
            GitHubPackageDownloader()
            mock_cred.assert_not_called()

    @patch("apm_cli.deps.github_downloader.Repo")
    @patch("tempfile.mkdtemp")
    def test_resolve_git_reference_branch(self, mock_mkdtemp, mock_repo_class):
        """Test resolving a branch reference."""
        # Setup mocks
        mock_temp_dir = "/tmp/test"
        mock_mkdtemp.return_value = mock_temp_dir

        mock_repo = Mock()
        mock_repo.head.commit.hexsha = "abc123def456"
        mock_repo_class.clone_from.return_value = mock_repo

        with patch("pathlib.Path.exists", return_value=True), patch("shutil.rmtree"):
            result = self.downloader.resolve_git_reference("user/repo#main")

            assert isinstance(result, ResolvedReference)
            assert result.original_ref == "github.com/user/repo#main"
            assert result.ref_type == GitReferenceType.BRANCH
            assert result.resolved_commit == "abc123def456"
            assert result.ref_name == "main"

    @patch("apm_cli.deps.github_downloader.Repo")
    @patch("tempfile.mkdtemp")
    def test_resolve_git_reference_commit(self, mock_mkdtemp, mock_repo_class):
        """Test resolving a commit SHA reference."""
        # Setup mocks for failed shallow clone, successful full clone
        mock_temp_dir = "/tmp/test"
        mock_mkdtemp.return_value = mock_temp_dir

        from git.exc import GitCommandError

        # First call (shallow clone) fails, second call (full clone) succeeds
        mock_repo = Mock()
        mock_commit = Mock()
        mock_commit.hexsha = "abcdef123456"
        mock_repo.commit.return_value = mock_commit

        mock_repo_class.clone_from.side_effect = [
            GitCommandError("shallow clone failed"),
            mock_repo,
        ]

        with patch("pathlib.Path.exists", return_value=True), patch("shutil.rmtree"):
            result = self.downloader.resolve_git_reference("user/repo#abcdef1")

            assert result.ref_type == GitReferenceType.COMMIT
            assert result.resolved_commit == "abcdef123456"
            assert result.ref_name == "abcdef1"

    @patch("apm_cli.deps.github_downloader.Repo")
    @patch("tempfile.mkdtemp")
    def test_resolve_git_reference_no_ref_uses_remote_head(self, mock_mkdtemp, mock_repo_class):
        """No #ref in dependency string should clone without --branch and detect the
        remote default branch, so repos that use 'master' or any other name work."""
        mock_temp_dir = "/tmp/test"
        mock_mkdtemp.return_value = mock_temp_dir

        mock_repo = Mock()
        mock_repo.head.commit.hexsha = "deadbeef1234"
        mock_repo.active_branch.name = "master"
        mock_repo_class.clone_from.return_value = mock_repo

        with patch("pathlib.Path.exists", return_value=True), patch("shutil.rmtree"):
            result = self.downloader.resolve_git_reference("user/repo")

            assert isinstance(result, ResolvedReference)
            assert result.ref_type == GitReferenceType.BRANCH
            assert result.resolved_commit == "deadbeef1234"
            assert result.ref_name == "master"

            # Verify clone was called without a 'branch' keyword argument
            call_kwargs = mock_repo_class.clone_from.call_args
            assert "branch" not in call_kwargs.kwargs

    def test_resolve_git_reference_invalid_format(self):
        """Test resolving an invalid repository reference."""
        with pytest.raises(ValueError, match="Invalid repository reference"):
            self.downloader.resolve_git_reference("invalid-repo-format")

    @patch("apm_cli.deps.github_downloader.Repo")
    @patch("apm_cli.deps.github_downloader.validate_apm_package")
    @patch("apm_cli.deps.github_downloader.shutil.rmtree")
    def test_download_package_success(self, mock_rmtree, mock_validate, mock_repo_class):
        """Test successful package download and validation."""
        # Setup target directory
        target_path = self.temp_dir / "test_package"

        # Setup mocks
        mock_repo = Mock()
        mock_repo_class.clone_from.return_value = mock_repo

        # Mock successful validation
        mock_validation_result = ValidationResult()
        mock_validation_result.is_valid = True
        mock_package = APMPackage(name="test-package", version="1.0.0")
        mock_validation_result.package = mock_package
        mock_validate.return_value = mock_validation_result

        # Mock resolve_git_reference
        mock_resolved_ref = ResolvedReference(
            original_ref="user/repo#main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        )

        with patch.object(self.downloader, "resolve_git_reference", return_value=mock_resolved_ref):
            result = self.downloader.download_package("user/repo#main", target_path)

            assert result.package.name == "test-package"
            assert result.package.version == "1.0.0"
            assert result.install_path == target_path
            assert result.resolved_reference == mock_resolved_ref
            assert result.installed_at is not None

    @patch("apm_cli.deps.github_downloader.Repo")
    @patch("apm_cli.deps.github_downloader.validate_apm_package")
    @patch("apm_cli.deps.github_downloader.shutil.rmtree")
    def test_download_package_validation_failure(self, mock_rmtree, mock_validate, mock_repo_class):
        """Test package download with validation failure."""
        # Setup target directory
        target_path = self.temp_dir / "test_package"

        # Setup mocks
        mock_repo = Mock()
        mock_repo_class.clone_from.return_value = mock_repo

        # Mock validation failure
        mock_validation_result = ValidationResult()
        mock_validation_result.is_valid = False
        mock_validation_result.add_error("Missing apm.yml")
        mock_validate.return_value = mock_validation_result

        # Mock resolve_git_reference
        mock_resolved_ref = ResolvedReference(
            original_ref="user/repo#main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        )

        with patch.object(self.downloader, "resolve_git_reference", return_value=mock_resolved_ref):
            with pytest.raises(RuntimeError, match="Invalid APM package"):
                self.downloader.download_package("user/repo#main", target_path)

    @patch("apm_cli.deps.github_downloader.Repo")
    def test_download_package_git_failure(self, mock_repo_class):
        """Test package download with Git clone failure."""
        # Setup target directory
        target_path = self.temp_dir / "test_package"

        # Setup mocks
        from git.exc import GitCommandError

        mock_repo_class.clone_from.side_effect = GitCommandError("Clone failed")

        # Mock resolve_git_reference
        mock_resolved_ref = ResolvedReference(
            original_ref="user/repo#main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abc123",
            ref_name="main",
        )

        with patch.object(self.downloader, "resolve_git_reference", return_value=mock_resolved_ref):
            with pytest.raises(RuntimeError, match="Failed to clone repository"):
                self.downloader.download_package("user/repo#main", target_path)

    def test_download_package_invalid_repo_ref(self):
        """Test package download with invalid repository reference."""
        target_path = self.temp_dir / "test_package"

        with pytest.raises(ValueError, match="Invalid repository reference"):
            self.downloader.download_package("invalid-repo-format", target_path)

    @patch("apm_cli.deps.github_downloader.Repo")
    @patch("apm_cli.deps.github_downloader.validate_apm_package")
    @patch("apm_cli.deps.github_downloader.shutil.rmtree")
    def test_download_package_commit_checkout(self, mock_rmtree, mock_validate, mock_repo_class):
        """Test package download with commit checkout."""
        # Setup target directory
        target_path = self.temp_dir / "test_package"

        # Setup mocks
        mock_repo = Mock()
        mock_repo.git = Mock()
        mock_repo_class.clone_from.return_value = mock_repo

        # Mock successful validation
        mock_validation_result = ValidationResult()
        mock_validation_result.is_valid = True
        mock_package = APMPackage(name="test-package", version="1.0.0")
        mock_validation_result.package = mock_package
        mock_validate.return_value = mock_validation_result

        # Mock resolve_git_reference returning a commit
        mock_resolved_ref = ResolvedReference(
            original_ref="user/repo#abc123",
            ref_type=GitReferenceType.COMMIT,
            resolved_commit="abc123def456",
            ref_name="abc123",
        )

        with patch.object(self.downloader, "resolve_git_reference", return_value=mock_resolved_ref):
            result = self.downloader.download_package("user/repo#abc123", target_path)

            # Verify that git checkout was called for commit
            mock_repo.git.checkout.assert_called_once_with("abc123def456")
            assert result.package.name == "test-package"

    def test_get_clone_progress_callback(self):
        """Test the progress callback for Git clone operations."""
        callback = self.downloader._get_clone_progress_callback()

        # Test with max_count
        with patch("builtins.print") as mock_print:
            callback(1, 50, 100, "Cloning")
            mock_print.assert_called_with("\r Cloning: 50% (50/100) Cloning", end="", flush=True)

        # Test without max_count
        with patch("builtins.print") as mock_print:
            callback(1, 25, None, "Receiving objects")
            mock_print.assert_called_with("\r Cloning: Receiving objects (25)", end="", flush=True)


class TestGitHubPackageDownloaderIntegration:
    """Integration tests that require actual Git operations (to be run with network access)."""

    def setup_method(self):
        """Set up test fixtures."""
        self.downloader = GitHubPackageDownloader()
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    @pytest.mark.integration
    def test_resolve_reference_real_repo(self):
        """Test resolving references on a real repository (requires network)."""
        # This test would require a real repository - skip in CI
        pytest.skip("Integration test requiring network access")

    @pytest.mark.integration
    def test_download_real_package(self):
        """Test downloading a real APM package (requires network)."""
        # This test would require a real APM package repository - skip in CI
        pytest.skip("Integration test requiring network access")


class TestEnterpriseHostHandling:
    """Test enterprise GitHub host handling (PR #33 bug fixes)."""

    @patch("apm_cli.deps.github_downloader.Repo")
    def test_clone_fallback_respects_enterprise_host(self, mock_repo_class, monkeypatch):
        """Test that fallback clone uses enterprise host, not hardcoded github.com.

        This tests the bug fix from PR #33 where Method 3 fallback was hardcoded
        to github.com instead of respecting the configured host.
        """
        from git.exc import GitCommandError

        monkeypatch.setenv("GITHUB_HOST", "company.ghe.com")
        monkeypatch.setenv("GITHUB_APM_PAT", "test-enterprise-token")

        downloader = GitHubPackageDownloader()
        downloader.github_host = "company.ghe.com"

        # Mock clone attempts: first two fail, third succeeds
        mock_repo = Mock()
        mock_repo.head.commit.hexsha = "abc123"

        mock_repo_class.clone_from.side_effect = [
            GitCommandError("auth", "Authentication failed"),  # Method 1 fails
            GitCommandError("ssh", "SSH failed"),  # Method 2 fails
            mock_repo,  # Method 3 succeeds
        ]

        target_path = Path("/tmp/test_enterprise")

        with patch("pathlib.Path.exists", return_value=False):
            result = downloader._clone_with_fallback("team/internal-repo", target_path)  # noqa: F841

        # Verify Method 3 used enterprise host, NOT github.com
        calls = mock_repo_class.clone_from.call_args_list
        assert len(calls) == 3

        third_call_url = calls[2][0][0]  # First positional arg of third call

        # Should use company.ghe.com, NOT github.com
        assert "company.ghe.com" in third_call_url
        assert "team/internal-repo" in third_call_url
        # Ensure it's NOT using github.com
        assert "github.com" not in third_call_url or "company.ghe.com" in third_call_url

    def test_host_persists_through_clone_attempts(self, monkeypatch):
        """Test that github_host attribute persists across fallback attempts."""
        monkeypatch.setenv("GITHUB_HOST", "custom.ghe.com")

        downloader = GitHubPackageDownloader()
        downloader.github_host = "custom.ghe.com"

        # Build URLs for both SSH and HTTPS methods
        url_ssh = downloader._build_repo_url("owner/repo", use_ssh=True)
        url_https = downloader._build_repo_url("owner/repo", use_ssh=False)

        assert "custom.ghe.com" in url_ssh
        assert "custom.ghe.com" in url_https
        assert "owner/repo" in url_https
        # Should NOT fall back to github.com
        assert "github.com" not in url_https or "custom.ghe.com" in url_https

    def test_multiple_hosts_resolution(self, monkeypatch):
        """Test installing packages from multiple GitHub hosts."""
        monkeypatch.setenv("GITHUB_HOST", "company.ghe.com")

        # Test bare dependency uses GITHUB_HOST
        dep1 = DependencyReference.parse("team/internal-package")
        assert dep1.repo_url == "team/internal-package"
        # Host should be set when downloader processes it

        # Test explicit github.com
        dep2 = DependencyReference.parse("github.com/public/open-source")
        assert dep2.host == "github.com"
        assert dep2.repo_url == "public/open-source"

        # Test explicit partner GHE
        dep3 = DependencyReference.parse("partner.ghe.com/external/tool")
        assert dep3.host == "partner.ghe.com"
        assert dep3.repo_url == "external/tool"


class TestErrorHandling:
    """Test error handling scenarios."""

    def test_network_timeout_handling(self):
        """Test handling of network timeouts."""
        # Would require mocking network timeouts
        pass

    def test_authentication_failure_handling(self):
        """Test handling of authentication failures."""
        # Would require mocking authentication failures
        pass

    def test_download_raw_file_saml_fallback_retries_without_token(self):
        """Test that download_raw_file retries without token on 401/403 (SAML/SSO)."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "saml-blocked-token"}, clear=True):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("microsoft/some-public-repo/sub/dir")

            # First call (with token) returns 401, second call (without token) returns 200
            mock_response_401 = Mock()
            mock_response_401.status_code = 401
            mock_response_401.raise_for_status = Mock(
                side_effect=requests_lib.exceptions.HTTPError(response=mock_response_401)
            )

            mock_response_200 = Mock()
            mock_response_200.status_code = 200
            mock_response_200.content = b"# SKILL.md content"
            mock_response_200.raise_for_status = Mock()

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                mock_get.side_effect = [mock_response_401, mock_response_200]

                result = downloader.download_raw_file(dep_ref, "sub/dir/SKILL.md", "main")
                assert result == b"# SKILL.md content"

                # First call should include auth header
                first_call_headers = mock_get.call_args_list[0][1].get("headers", {})
                assert "Authorization" in first_call_headers

                # Second (retry) call should NOT include auth header
                second_call_headers = mock_get.call_args_list[1][1].get("headers", {})
                assert "Authorization" not in second_call_headers

    def test_download_raw_file_saml_fallback_not_used_for_ghe_cloud_dr(self):
        """Test that SAML fallback does NOT apply to *.ghe.com (no public repos)."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "ghe-token"}, clear=True):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("company.ghe.com/owner/repo/sub/path")

            mock_response_403 = Mock()
            mock_response_403.status_code = 403
            mock_response_403.raise_for_status = Mock(
                side_effect=requests_lib.exceptions.HTTPError(response=mock_response_403)
            )

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                mock_get.return_value = mock_response_403

                with pytest.raises(RuntimeError, match="Authentication failed"):
                    downloader.download_raw_file(dep_ref, "sub/path/file.md", "main")

                # Should only have been called once — no retry for *.ghe.com
                assert mock_get.call_count == 1

    def test_download_raw_file_saml_fallback_applies_to_ghes(self):
        """Test that SAML fallback DOES apply to GHES custom domains (can have public repos)."""
        with patch.dict(
            os.environ,
            {"GITHUB_APM_PAT": "ghes-token", "GITHUB_HOST": "github.mycompany.com"},
            clear=True,
        ):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("github.mycompany.com/owner/repo/sub/path")

            mock_response_401 = Mock()
            mock_response_401.status_code = 401
            mock_response_401.raise_for_status = Mock(
                side_effect=requests_lib.exceptions.HTTPError(response=mock_response_401)
            )

            mock_response_200 = Mock()
            mock_response_200.status_code = 200
            mock_response_200.content = b"# Public GHES content"
            mock_response_200.raise_for_status = Mock()

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                mock_get.side_effect = [mock_response_401, mock_response_200]

                result = downloader.download_raw_file(dep_ref, "sub/path/SKILL.md", "main")
                assert result == b"# Public GHES content"

                # Should have retried without auth
                assert mock_get.call_count == 2
                second_call_headers = mock_get.call_args_list[1][1].get("headers", {})
                assert "Authorization" not in second_call_headers

    def test_download_raw_file_saml_fallback_retries_and_still_fails(self):
        """Test that when both authenticated and unauthenticated attempts fail, an error is raised."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "saml-blocked-token"}, clear=True):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("microsoft/private-repo/sub/dir")

            mock_response_401_first = Mock()
            mock_response_401_first.status_code = 401
            mock_response_401_first.raise_for_status = Mock(
                side_effect=requests_lib.exceptions.HTTPError(response=mock_response_401_first)
            )

            mock_response_401_second = Mock()
            mock_response_401_second.status_code = 401
            mock_response_401_second.raise_for_status = Mock(
                side_effect=requests_lib.exceptions.HTTPError(response=mock_response_401_second)
            )

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                mock_get.side_effect = [mock_response_401_first, mock_response_401_second]

                with pytest.raises(RuntimeError, match="Authentication failed"):
                    downloader.download_raw_file(dep_ref, "sub/dir/SKILL.md", "main")

                # Both attempts should have been made
                assert mock_get.call_count == 2

                # First call should include auth header
                first_call_headers = mock_get.call_args_list[0][1].get("headers", {})
                assert "Authorization" in first_call_headers

                # Second (retry) call should NOT include auth header
                second_call_headers = mock_get.call_args_list[1][1].get("headers", {})
                assert "Authorization" not in second_call_headers

    def test_repository_not_found_handling(self):
        """Test handling of repository not found errors."""
        # Would require mocking 404 errors
        pass

    def test_download_github_file_403_rate_limit_no_token(self):
        """Test that 403 with X-RateLimit-Remaining: 0 and no token gives a rate-limit error."""
        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse(
                "github/awesome-copilot/agents/api-architect.agent.md"
            )

            mock_response_403 = Mock()
            mock_response_403.status_code = 403
            mock_response_403.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}
            mock_response_403.raise_for_status = Mock(
                side_effect=requests_lib.exceptions.HTTPError(response=mock_response_403)
            )

            with (
                patch("apm_cli.deps.github_downloader.requests.get") as mock_get,
                patch("apm_cli.deps.github_downloader.time.sleep"),
            ):
                # _resilient_get retries 3 times on rate-limit 403, all return same
                mock_get.return_value = mock_response_403

                with pytest.raises(RuntimeError, match="rate limit exceeded") as exc_info:
                    downloader.download_raw_file(dep_ref, "agents/api-architect.agent.md", "main")

                # Must NOT mention "private repository" — that's the old misleading message
                assert "private repository" not in str(exc_info.value).lower()
                assert "60/hour" in str(exc_info.value)

    def test_download_github_file_403_rate_limit_with_token(self):
        """Test that 403 with X-RateLimit-Remaining: 0 and a token gives a rate-limit error."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "my-token"}, clear=True):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse(
                "github/awesome-copilot/agents/api-architect.agent.md"
            )

            mock_response_403 = Mock()
            mock_response_403.status_code = 403
            mock_response_403.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}
            mock_response_403.raise_for_status = Mock(
                side_effect=requests_lib.exceptions.HTTPError(response=mock_response_403)
            )

            with (
                patch("apm_cli.deps.github_downloader.requests.get") as mock_get,
                patch("apm_cli.deps.github_downloader.time.sleep"),
            ):
                mock_get.return_value = mock_response_403

                with pytest.raises(RuntimeError, match="rate limit exceeded") as exc_info:
                    downloader.download_raw_file(dep_ref, "agents/api-architect.agent.md", "main")

                assert "Authenticated rate limit exhausted" in str(exc_info.value)
                assert "SSO/SAML" not in str(exc_info.value)

    def test_download_github_file_403_non_rate_limit_still_auth_error(self):
        """Test that 403 WITHOUT rate-limit headers still produces the auth error."""
        with patch.dict(os.environ, {}, clear=True):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("owner/private-repo/sub/file.agent.md")

            mock_response_403 = Mock()
            mock_response_403.status_code = 403
            # No rate-limit headers — this is a genuine auth failure
            mock_response_403.headers = {}
            mock_response_403.raise_for_status = Mock(
                side_effect=requests_lib.exceptions.HTTPError(response=mock_response_403)
            )

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                mock_get.return_value = mock_response_403

                with pytest.raises(RuntimeError, match="Authentication failed"):
                    downloader.download_raw_file(dep_ref, "sub/file.agent.md", "main")

    def test_resilient_get_retries_on_403_rate_limit(self):
        """Test that _resilient_get retries when 403 has X-RateLimit-Remaining: 0."""
        with patch.dict(os.environ, {}, clear=True):
            downloader = GitHubPackageDownloader()

            mock_response_403 = Mock()
            mock_response_403.status_code = 403
            mock_response_403.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}

            mock_response_200 = Mock()
            mock_response_200.status_code = 200
            mock_response_200.headers = {"X-RateLimit-Remaining": "50"}
            mock_response_200.content = b"success"

            with (
                patch("apm_cli.deps.github_downloader.requests.get") as mock_get,
                patch("apm_cli.deps.github_downloader.time.sleep"),
            ):
                mock_get.side_effect = [mock_response_403, mock_response_200]

                response = downloader._resilient_get("https://api.github.com/repos/test", {})
                assert response.status_code == 200
                assert mock_get.call_count == 2

    def test_resilient_get_does_not_retry_403_without_rate_limit_header(self):
        """Test that _resilient_get does NOT retry 403 without rate-limit exhaustion."""
        with patch.dict(os.environ, {}, clear=True):
            downloader = GitHubPackageDownloader()

            mock_response_403 = Mock()
            mock_response_403.status_code = 403
            mock_response_403.headers = {}  # No rate-limit headers

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                mock_get.return_value = mock_response_403

                response = downloader._resilient_get("https://api.github.com/repos/test", {})
                # Should return immediately — no retry for non-rate-limit 403
                assert response.status_code == 403
                assert mock_get.call_count == 1

    def test_resilient_get_403_with_nonzero_remaining_not_retried(self):
        """Test that 403 with X-RateLimit-Remaining > 0 is NOT retried as rate limiting."""
        with patch.dict(os.environ, {}, clear=True):
            downloader = GitHubPackageDownloader()

            mock_response_403 = Mock()
            mock_response_403.status_code = 403
            mock_response_403.headers = {"X-RateLimit-Remaining": "42"}

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                mock_get.return_value = mock_response_403

                response = downloader._resilient_get("https://api.github.com/repos/test", {})
                assert response.status_code == 403
                assert mock_get.call_count == 1


class TestAzureDevOpsSupport:
    """Test Azure DevOps package support."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_setup_git_environment_with_ado_token(self):
        """Test Git environment setup picks up ADO_APM_PAT."""
        with patch.dict(os.environ, {"ADO_APM_PAT": "ado-test-token"}, clear=True):
            downloader = GitHubPackageDownloader()

            assert downloader.ado_token == "ado-test-token"
            assert downloader.has_ado_token is True

    def test_setup_git_environment_no_ado_token(self):
        """Test Git environment setup without ADO token."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "github-token"}, clear=True):
            downloader = GitHubPackageDownloader()

            assert downloader.ado_token is None
            assert downloader.has_ado_token is False
            # GitHub token should still work
            assert downloader.github_token == "github-token"
            assert downloader.has_github_token is True

    def test_setup_git_environment_sets_ssh_connect_timeout(self):
        """Git env should set GIT_SSH_COMMAND with ConnectTimeout when unset."""
        with patch.dict(os.environ, {}, clear=True):
            downloader = GitHubPackageDownloader()
            env = downloader.git_env

            assert "GIT_SSH_COMMAND" in env
            assert "ConnectTimeout=30" in env["GIT_SSH_COMMAND"]
            assert env["GIT_SSH_COMMAND"].startswith("ssh ")

    def test_setup_git_environment_merges_existing_ssh_command(self):
        """Git env should append ConnectTimeout to an existing GIT_SSH_COMMAND."""
        with patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -i ~/.ssh/custom_key"}, clear=True):
            downloader = GitHubPackageDownloader()
            env = downloader.git_env

            assert "ConnectTimeout=30" in env["GIT_SSH_COMMAND"]
            assert "-i ~/.ssh/custom_key" in env["GIT_SSH_COMMAND"]

    def test_setup_git_environment_preserves_existing_connect_timeout(self):
        """Git env should not duplicate ConnectTimeout if already present."""
        with patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -o ConnectTimeout=60"}, clear=True):
            downloader = GitHubPackageDownloader()
            env = downloader.git_env

            assert env["GIT_SSH_COMMAND"] == "ssh -o ConnectTimeout=60"

    def test_build_repo_url_for_ado_with_token(self):
        """Test URL building for ADO packages with token."""
        with patch.dict(os.environ, {"ADO_APM_PAT": "ado-token"}, clear=True):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("dev.azure.com/myorg/myproject/_git/myrepo")

            url = downloader._build_repo_url(dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref)
            parsed = urlparse(url)

            # Should build ADO URL with token embedded in userinfo
            assert parsed.hostname == "dev.azure.com"
            assert "myorg" in parsed.path
            assert "myproject" in parsed.path
            assert "_git" in parsed.path
            assert "myrepo" in parsed.path
            # Token should be in the URL (as username in https://token@host format)
            assert parsed.username == "ado-token" or "ado-token" in (parsed.password or "")

    def test_build_repo_url_for_ado_without_token(self):
        """Test URL building for ADO packages without token."""
        with patch.dict(os.environ, {}, clear=True):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("dev.azure.com/myorg/myproject/_git/myrepo")

            url = downloader._build_repo_url(dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref)
            parsed = urlparse(url)

            # Should build ADO URL without token
            assert parsed.hostname == "dev.azure.com"
            assert "myorg/myproject/_git/myrepo" in parsed.path
            # No credentials in URL
            assert parsed.username is None
            assert parsed.password is None

    def test_build_repo_url_for_ado_ssh(self):
        """Test SSH URL building for ADO packages."""
        with patch.dict(os.environ, {}, clear=True):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("dev.azure.com/myorg/myproject/_git/myrepo")

            url = downloader._build_repo_url(dep_ref.repo_url, use_ssh=True, dep_ref=dep_ref)

            # Should build ADO SSH URL (git@ssh.dev.azure.com:v3/org/project/repo)
            assert url.startswith("git@ssh.dev.azure.com:")

    def test_build_ado_urls_with_spaces_in_project(self):
        """Test that URL builders properly encode spaces in ADO project names."""
        from apm_cli.utils.github_host import (
            build_ado_api_url,
            build_ado_https_clone_url,
            build_ado_ssh_url,
        )

        # HTTPS clone URL with token
        url = build_ado_https_clone_url("myorg", "My Project", "myrepo", token="tok")
        assert "My%20Project" in url
        assert "My Project" not in url
        assert url == "https://tok@dev.azure.com/myorg/My%20Project/_git/myrepo"

        # HTTPS clone URL without token
        url = build_ado_https_clone_url("myorg", "My Project", "myrepo")
        assert url == "https://dev.azure.com/myorg/My%20Project/_git/myrepo"

        # SSH cloud URL
        url = build_ado_ssh_url("myorg", "My Project", "myrepo")
        assert "My%20Project" in url
        assert url == "git@ssh.dev.azure.com:v3/myorg/My%20Project/myrepo"

        # SSH server URL
        url = build_ado_ssh_url("myorg", "My Project", "myrepo", host="ado.company.com")
        assert "My%20Project" in url

        # API URL
        url = build_ado_api_url("myorg", "My Project", "myrepo", "path/file.md")
        assert "My%20Project" in url
        assert "My Project" not in url

    def test_build_repo_url_github_not_affected_by_ado_token(self):
        """Test that GitHub URL building uses GitHub token, not ADO token."""
        with patch.dict(
            os.environ, {"GITHUB_APM_PAT": "github-token", "ADO_APM_PAT": "ado-token"}, clear=True
        ):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("owner/repo")

            url = downloader._build_repo_url(dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref)
            parsed = urlparse(url)

            # Should use GitHub token, not ADO token
            assert parsed.hostname == "github.com"
            # Verify ADO token is not used for GitHub URLs
            assert "ado-token" not in url and parsed.username != "ado-token"

    def test_clone_with_fallback_selects_ado_token(self):
        """Test that _clone_with_fallback uses ADO token for ADO packages."""
        with patch.dict(
            os.environ, {"GITHUB_APM_PAT": "github-token", "ADO_APM_PAT": "ado-token"}, clear=True
        ):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("dev.azure.com/myorg/myproject/_git/myrepo")

            # Mock _build_repo_url to capture what's passed
            with patch.object(downloader, "_build_repo_url") as mock_build:
                mock_build.return_value = (
                    "https://ado-token@dev.azure.com/myorg/myproject/_git/myrepo"
                )

                with patch("apm_cli.deps.github_downloader.Repo") as mock_repo:
                    mock_repo.clone_from.return_value = Mock()

                    try:  # noqa: SIM105
                        downloader._clone_with_fallback(
                            dep_ref.repo_url, self.temp_dir, dep_ref=dep_ref
                        )
                    except Exception:
                        pass  # May fail due to mocking, we just want to check the call

                    # Verify _build_repo_url was called with dep_ref
                    if mock_build.called:
                        call_args = mock_build.call_args
                        assert call_args[1].get("dep_ref") is not None

    def test_clone_with_fallback_selects_github_token(self):
        """Test that _clone_with_fallback uses GitHub token for GitHub packages."""
        with patch.dict(
            os.environ, {"GITHUB_APM_PAT": "github-token", "ADO_APM_PAT": "ado-token"}, clear=True
        ):
            dep_ref = DependencyReference.parse("owner/repo")

            # The is_ado check should be False for GitHub packages
            assert not dep_ref.is_azure_devops()


class TestMixedSourceTokenSelection:
    """Test token selection for mixed-source installations (GitHub.com + GHE + ADO)."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mixed_tokens_github_com(self):
        """Test that github.com packages use GITHUB_APM_PAT."""
        with patch.dict(
            os.environ, {"GITHUB_APM_PAT": "github-token", "ADO_APM_PAT": "ado-token"}, clear=True
        ):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("github.com/owner/repo")

            url = downloader._build_repo_url(dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref)
            parsed = urlparse(url)

            assert parsed.hostname == "github.com"
            # GitHub token should be present, ADO token should not
            assert "ado-token" not in url and parsed.username != "ado-token"

    def test_mixed_tokens_ghe(self):
        """Test that GHE packages use GITHUB_APM_PAT."""
        with patch.dict(
            os.environ, {"GITHUB_APM_PAT": "github-token", "ADO_APM_PAT": "ado-token"}, clear=True
        ):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("octodemo-eu.ghe.com/owner/repo")

            url = downloader._build_repo_url(dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref)
            parsed = urlparse(url)

            assert parsed.hostname == "octodemo-eu.ghe.com"
            # ADO token should not be used for GHE
            assert "ado-token" not in url and parsed.username != "ado-token"

    def test_mixed_tokens_ado(self):
        """Test that ADO packages use ADO_APM_PAT."""
        with patch.dict(
            os.environ, {"GITHUB_APM_PAT": "github-token", "ADO_APM_PAT": "ado-token"}, clear=True
        ):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("dev.azure.com/myorg/myproject/_git/myrepo")

            url = downloader._build_repo_url(dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref)
            parsed = urlparse(url)

            assert parsed.hostname == "dev.azure.com"
            # ADO token should be used (as username), GitHub token should not
            assert parsed.username == "ado-token" or "ado-token" in (parsed.password or "")
            assert "github-token" not in url

    def test_mixed_tokens_bare_owner_repo_with_github_host(self):
        """Test bare owner/repo uses GITHUB_HOST and GITHUB_APM_PAT."""
        with patch.dict(
            os.environ,
            {
                "GITHUB_APM_PAT": "github-token",
                "ADO_APM_PAT": "ado-token",
                "GITHUB_HOST": "company.ghe.com",
            },
            clear=True,
        ):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("owner/repo")

            # Simulate resolution to custom host
            # The dep_ref.host will be github.com by default, but GITHUB_HOST
            # affects the actual URL building in the downloader
            url = downloader._build_repo_url(dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref)
            parsed = urlparse(url)

            # Should use GitHub token for GitHub-family hosts, not ADO token
            assert "ado-token" not in url and parsed.username != "ado-token"

    def test_mixed_installation_token_isolation(self):
        """Test that tokens are isolated per platform in mixed installation."""
        with patch.dict(
            os.environ, {"GITHUB_APM_PAT": "github-token", "ADO_APM_PAT": "ado-token"}, clear=True
        ):
            downloader = GitHubPackageDownloader()

            # Parse multiple deps from different sources
            github_dep = DependencyReference.parse("github.com/owner/repo")
            ghe_dep = DependencyReference.parse("company.ghe.com/owner/repo")
            ado_dep = DependencyReference.parse("dev.azure.com/org/proj/_git/repo")

            # Build URLs for each
            github_url = downloader._build_repo_url(
                github_dep.repo_url, use_ssh=False, dep_ref=github_dep
            )
            ghe_url = downloader._build_repo_url(ghe_dep.repo_url, use_ssh=False, dep_ref=ghe_dep)
            ado_url = downloader._build_repo_url(ado_dep.repo_url, use_ssh=False, dep_ref=ado_dep)

            github_parsed = urlparse(github_url)
            ghe_parsed = urlparse(ghe_url)
            ado_parsed = urlparse(ado_url)

            # Verify correct hosts
            assert github_parsed.hostname == "github.com"
            assert ghe_parsed.hostname == "company.ghe.com"
            assert ado_parsed.hostname == "dev.azure.com"

            # Verify token isolation - ADO token only in ADO URL
            assert "ado-token" not in github_url
            assert "ado-token" not in ghe_url
            assert ado_parsed.username == "ado-token" or "ado-token" in (ado_parsed.password or "")

            # Verify GitHub token not in ADO URL
            assert "github-token" not in ado_url

    def test_github_ado_without_ado_token_falls_back(self):
        """Test ADO without token still builds valid URL."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "github-token"}, clear=True):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("dev.azure.com/myorg/myproject/_git/myrepo")

            url = downloader._build_repo_url(dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref)
            parsed = urlparse(url)

            # Should build valid ADO URL without auth
            assert parsed.hostname == "dev.azure.com"
            assert "myorg/myproject/_git/myrepo" in parsed.path
            # GitHub token should NOT be used for ADO - no credentials at all
            assert parsed.username is None or parsed.username != "github-token"
            assert "github-token" not in url

    def test_ghe_without_github_token_falls_back(self):
        """Test GHE without token still builds valid URL."""
        with patch.dict(os.environ, {"ADO_APM_PAT": "ado-token"}, clear=True):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("company.ghe.com/owner/repo")

            url = downloader._build_repo_url(dep_ref.repo_url, use_ssh=False, dep_ref=dep_ref)
            parsed = urlparse(url)

            # Should build valid GHE URL without auth
            assert parsed.hostname == "company.ghe.com"
            assert "owner/repo" in parsed.path
            # ADO token should NOT be used for GHE - no credentials at all
            assert parsed.username is None or parsed.username != "ado-token"
            assert "ado-token" not in url


class TestSubdirectoryPackageCommitSHA:
    """Test commit SHA handling in download_subdirectory_package."""

    def setup_method(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_dep_ref(self, ref=None):
        """Create a virtual subdirectory DependencyReference."""
        dep = DependencyReference(
            repo_url="owner/monorepo",
            host="github.com",
            reference=ref,
            virtual_path="packages/my-skill",
            is_virtual=True,
        )
        return dep

    @patch("apm_cli.deps.github_downloader.Repo")
    @patch("apm_cli.deps.github_downloader.validate_apm_package")
    def test_sha_ref_clones_without_depth_and_checks_out(self, mock_validate, mock_repo_class):
        """Commit SHA refs must clone with no_checkout (no depth/branch) then checkout the SHA."""
        sha = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        dep_ref = self._make_dep_ref(ref=sha)

        mock_repo = Mock()
        mock_repo_class.return_value = mock_repo

        mock_validation = ValidationResult()
        mock_validation.is_valid = True
        mock_validation.package = APMPackage(name="my-skill", version="1.0.0")
        mock_validate.return_value = mock_validation

        with patch.dict(os.environ, {}, clear=True):
            downloader = GitHubPackageDownloader()

        with patch.object(downloader, "_clone_with_fallback") as mock_clone:
            mock_clone.return_value = mock_repo

            target = self.temp_dir / "my-skill"

            # Create the subdirectory structure that download_subdirectory_package expects
            def setup_subdir(*args, **kwargs):
                clone_path = args[1]
                subdir = clone_path / "packages" / "my-skill"
                subdir.mkdir(parents=True)
                (subdir / "apm.yml").write_text("name: my-skill\nversion: 1.0.0\n")
                return mock_repo

            mock_clone.side_effect = setup_subdir

            downloader.download_subdirectory_package(dep_ref, target)

            # Verify clone was called without depth/branch but WITH no_checkout
            call_kwargs = mock_clone.call_args
            assert "depth" not in call_kwargs.kwargs, "SHA ref should NOT use shallow clone"
            assert "branch" not in call_kwargs.kwargs, "SHA ref should NOT pass branch"
            assert call_kwargs.kwargs.get("no_checkout") is True, (
                "SHA ref should use no_checkout=True"
            )

            # Verify checkout was called with the SHA
            mock_repo.git.checkout.assert_called_once_with(sha)

    @patch("apm_cli.deps.github_downloader.Repo")
    @patch("apm_cli.deps.github_downloader.validate_apm_package")
    def test_branch_ref_uses_shallow_clone(self, mock_validate, mock_repo_class):
        """Branch/tag refs must use shallow clone with depth=1 and branch kwarg."""
        dep_ref = self._make_dep_ref(ref="main")

        mock_repo = Mock()
        mock_repo_class.return_value = mock_repo

        mock_validation = ValidationResult()
        mock_validation.is_valid = True
        mock_validation.package = APMPackage(name="my-skill", version="1.0.0")
        mock_validate.return_value = mock_validation

        with patch.dict(os.environ, {}, clear=True):
            downloader = GitHubPackageDownloader()

        with patch.object(downloader, "_clone_with_fallback") as mock_clone:
            mock_clone.return_value = mock_repo

            target = self.temp_dir / "my-skill"

            def setup_subdir(*args, **kwargs):
                clone_path = args[1]
                subdir = clone_path / "packages" / "my-skill"
                subdir.mkdir(parents=True)
                (subdir / "apm.yml").write_text("name: my-skill\nversion: 1.0.0\n")
                return mock_repo

            mock_clone.side_effect = setup_subdir

            downloader.download_subdirectory_package(dep_ref, target)

            call_kwargs = mock_clone.call_args
            assert call_kwargs.kwargs.get("depth") == 1, "Branch ref should use depth=1"
            assert call_kwargs.kwargs.get("branch") == "main", "Branch ref should pass branch"
            assert "no_checkout" not in call_kwargs.kwargs, "Branch ref should not set no_checkout"

            # No explicit checkout for branch refs
            mock_repo.git.checkout.assert_not_called()

    @patch("apm_cli.deps.github_downloader.Repo")
    @patch("apm_cli.deps.github_downloader.validate_apm_package")
    def test_no_ref_uses_shallow_clone_without_branch(self, mock_validate, mock_repo_class):
        """No ref should use shallow clone without branch kwarg (default branch)."""
        dep_ref = self._make_dep_ref(ref=None)

        mock_repo = Mock()
        mock_repo_class.return_value = mock_repo

        mock_validation = ValidationResult()
        mock_validation.is_valid = True
        mock_validation.package = APMPackage(name="my-skill", version="1.0.0")
        mock_validate.return_value = mock_validation

        with patch.dict(os.environ, {}, clear=True):
            downloader = GitHubPackageDownloader()

        with patch.object(downloader, "_clone_with_fallback") as mock_clone:
            mock_clone.return_value = mock_repo

            target = self.temp_dir / "my-skill"

            def setup_subdir(*args, **kwargs):
                clone_path = args[1]
                subdir = clone_path / "packages" / "my-skill"
                subdir.mkdir(parents=True)
                (subdir / "apm.yml").write_text("name: my-skill\nversion: 1.0.0\n")
                return mock_repo

            mock_clone.side_effect = setup_subdir

            downloader.download_subdirectory_package(dep_ref, target)

            call_kwargs = mock_clone.call_args
            assert call_kwargs.kwargs.get("depth") == 1, "No ref should still shallow clone"
            assert "branch" not in call_kwargs.kwargs, "No ref should not pass branch"

    @patch("apm_cli.deps.github_downloader.Repo")
    def test_sha_checkout_failure_raises_descriptive_error(self, mock_repo_class):
        """Checkout failure for SHA ref should raise error mentioning 'checkout', not 'clone'."""
        sha = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        dep_ref = self._make_dep_ref(ref=sha)

        mock_repo = Mock()
        mock_repo.git.checkout.side_effect = Exception("bad object a1b2c3d")
        mock_repo_class.return_value = mock_repo

        with patch.dict(os.environ, {}, clear=True):
            downloader = GitHubPackageDownloader()

        with patch.object(downloader, "_clone_with_fallback") as mock_clone:

            def setup_subdir(*args, **kwargs):
                clone_path = args[1]
                subdir = clone_path / "packages" / "my-skill"
                subdir.mkdir(parents=True)
                return mock_repo

            mock_clone.side_effect = setup_subdir

            target = self.temp_dir / "my-skill"
            with pytest.raises(RuntimeError, match="Failed to checkout commit"):
                downloader.download_subdirectory_package(dep_ref, target)


class TestWindowsCleanupHelpers:
    """Test _rmtree and _close_repo helpers for Windows compatibility."""

    def test_rmtree_removes_normal_directory(self):
        from apm_cli.deps.github_downloader import _rmtree

        d = Path(tempfile.mkdtemp())
        (d / "file.txt").write_text("hello")
        _rmtree(d)
        assert not d.exists()

    def test_rmtree_handles_readonly_files(self):
        from apm_cli.deps.github_downloader import _rmtree

        d = Path(tempfile.mkdtemp())
        f = d / "readonly.txt"
        f.write_text("locked")
        os.chmod(str(f), stat.S_IREAD)
        _rmtree(d)
        assert not d.exists()

    def test_close_repo_none_is_safe(self):
        from apm_cli.deps.github_downloader import _close_repo

        # Must not raise when passed None
        _close_repo(None)

    def test_close_repo_releases_gitpython_handles(self):
        from apm_cli.deps.github_downloader import _close_repo

        repo = MagicMock()
        _close_repo(repo)
        repo.git.clear_cache.assert_called_once()
        repo.close.assert_called_once()

    def test_close_repo_swallows_exceptions(self):
        from apm_cli.deps.github_downloader import _close_repo

        repo = MagicMock()
        repo.git.clear_cache.side_effect = RuntimeError("git gone")
        # Must not propagate
        _close_repo(repo)
        # Even if clear_cache fails, we must still attempt it and close the repo
        repo.git.clear_cache.assert_called_once()
        repo.close.assert_called_once()


class TestDownloadSubdirectoryPackageWindowsCleanup:
    """Verify that WinError 32 file-lock races don't surface to the caller.

    The root issue on Windows is that TemporaryDirectory.__exit__ calls
    shutil.rmtree without retry logic, and git subprocess handles may still
    be alive when the cleanup runs.  The fix: manual mkdtemp + try/finally
    + _rmtree, plus _close_repo() before cleanup.
    """

    def _make_dep_ref(self):
        """Return a minimal DependencyReference for a subdirectory package."""
        # owner/repo/skills/test-pkg → virtual subdirectory reference
        return DependencyReference.parse("owner/repo/skills/test-pkg")

    def test_sparse_checkout_success_closes_sha_repo_before_rmtree(self, tmp_path):
        """When sparse checkout succeeds the SHA-capture Repo is closed before _rmtree."""
        from apm_cli.deps.github_downloader import _close_repo  # noqa: F401

        downloader = GitHubPackageDownloader()
        dep = self._make_dep_ref()
        target = tmp_path / "out"

        call_order = []

        def fake_close_repo(repo):
            if repo is not None:
                call_order.append(("close_repo", repo))

        def fake_rmtree(path):
            call_order.append(("rmtree", path))

        fake_repo = MagicMock()
        fake_repo.head.commit.hexsha = "abc1234"

        def fake_sparse(dep_ref, clone_path, subdir, ref):
            # Simulate sparse checkout writing the subdir
            (clone_path / subdir).mkdir(parents=True, exist_ok=True)
            (clone_path / subdir / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n")
            return True

        with (
            patch("apm_cli.deps.github_downloader._close_repo", side_effect=fake_close_repo),
            patch("apm_cli.deps.github_downloader._rmtree", side_effect=fake_rmtree),
            patch.object(downloader, "_try_sparse_checkout", side_effect=fake_sparse),
            patch("apm_cli.deps.github_downloader.Repo", return_value=fake_repo),
            patch("apm_cli.deps.github_downloader.validate_apm_package") as mock_validate,
        ):
            mock_validate.return_value = MagicMock(is_valid=True, errors=[])
            downloader.download_subdirectory_package(dep, target)

        # Verify both _close_repo and _rmtree were called
        assert any(op == "close_repo" for op, _ in call_order), "_close_repo was not called"
        assert any(op == "rmtree" for op, _ in call_order), "_rmtree was not called"
        # Verify _close_repo was called before _rmtree
        close_repo_idx = next(i for i, (op, _) in enumerate(call_order) if op == "close_repo")
        rmtree_idx = next(i for i, (op, _) in enumerate(call_order) if op == "rmtree")
        assert close_repo_idx < rmtree_idx, "_close_repo must be called before _rmtree"

    def test_sparse_checkout_failure_uses_fresh_clone_path(self, tmp_path):
        """When sparse checkout fails the full clone goes to a fresh path (repo_clone/)."""
        downloader = GitHubPackageDownloader()
        dep = self._make_dep_ref()
        target = tmp_path / "out"

        cloned_paths = []

        def fake_clone_with_fallback(url, path, progress_reporter=None, **kwargs):
            cloned_paths.append(path)
            # Simulate clone writing the subdir
            (path / dep.virtual_path).mkdir(parents=True, exist_ok=True)
            (path / dep.virtual_path / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n")

        fake_repo = MagicMock()
        fake_repo.head.commit.hexsha = "abc1234"

        with (
            patch.object(downloader, "_try_sparse_checkout", return_value=False),
            patch.object(downloader, "_clone_with_fallback", side_effect=fake_clone_with_fallback),
            patch("apm_cli.deps.github_downloader.Repo", return_value=fake_repo),
            patch("apm_cli.deps.github_downloader._close_repo"),
            patch("apm_cli.deps.github_downloader.validate_apm_package") as mock_validate,
        ):
            mock_validate.return_value = MagicMock(is_valid=True, errors=[])
            downloader.download_subdirectory_package(dep, target)

        # Full clone must NOT reuse the sparse-checkout path "repo/"
        assert len(cloned_paths) == 1
        assert cloned_paths[0].name == "repo_clone"


class TestGitEnvironmentPlatformBehavior:
    """Test platform-specific behavior in Git environment setup."""

    def test_git_config_global_uses_empty_file_on_windows(self):
        """GIT_CONFIG_GLOBAL should be an existing empty file on Windows (not NUL)."""
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "tok"}, clear=True),
            patch("sys.platform", "win32"),
        ):
            dl = GitHubPackageDownloader()
            cfg_path = dl.git_env["GIT_CONFIG_GLOBAL"]
            # Must be a real path (not 'NUL') that exists as a file
            assert cfg_path != "NUL"
            assert os.path.isfile(cfg_path)

    def test_git_config_global_uses_dev_null_on_unix(self):
        """GIT_CONFIG_GLOBAL should be '/dev/null' on Unix."""
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "tok"}, clear=True),
            patch("sys.platform", "darwin"),
        ):
            dl = GitHubPackageDownloader()
            assert dl.git_env["GIT_CONFIG_GLOBAL"] == "/dev/null"


class TestDownloaderCredentialFallback:
    """Test credential fallback behavior in GitHubPackageDownloader."""

    def test_credential_fill_used_when_no_env_token(self):
        """When no env tokens are set, credential helpers should be used."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value="credential-token",
            ),
        ):
            downloader = GitHubPackageDownloader()
            assert downloader.github_token == "credential-token"
            assert downloader._github_token_from_credential_fill is True

    def test_env_token_takes_priority_over_credential_fill(self):
        """GITHUB_APM_PAT should take priority over credential helpers."""
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "apm-pat-token"}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
            ) as mock_cred,
        ):
            downloader = GitHubPackageDownloader()
            assert downloader.github_token == "apm-pat-token"
            assert downloader._github_token_from_credential_fill is False
            mock_cred.assert_not_called()

    def test_credential_fill_for_non_default_host(self):
        """Non-default hosts should try credential fill on demand in _download_github_file."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
            ) as mock_cred,
        ):
            # Return None for default host, enterprise token for custom host
            mock_cred.side_effect = lambda host, port=None: (
                "enterprise-token" if host == "ghes.company.com" else None
            )
            downloader = GitHubPackageDownloader()
            # No token for default host
            assert downloader.github_token is None

            dep_ref = DependencyReference(
                repo_url="owner/repo",
                host="ghes.company.com",
            )

            mock_response_200 = Mock()
            mock_response_200.status_code = 200
            mock_response_200.content = b"file content"
            mock_response_200.raise_for_status = Mock()

            with patch.object(
                downloader, "_resilient_get", return_value=mock_response_200
            ) as mock_get:
                result = downloader._download_github_file(dep_ref, "SKILL.md", "main")
                assert result == b"file content"

                call_headers = mock_get.call_args[1].get(  # noqa: F841
                    "headers", mock_get.call_args[0][1] if len(mock_get.call_args[0]) > 1 else {}
                )
                # _resilient_get is called as (url, headers=headers, timeout=30)
                actual_headers = mock_get.call_args[1].get("headers") or mock_get.call_args[0][1]
                assert actual_headers.get("Authorization") == "token enterprise-token"

    def test_non_default_host_uses_global_token(self):
        """Global env vars (GITHUB_APM_PAT) are now tried for all hosts, not just the default."""
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "default-host-pat"}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
            ) as mock_cred,
        ):
            mock_cred.return_value = "enterprise-cred"
            downloader = GitHubPackageDownloader()
            assert downloader.github_token == "default-host-pat"

            dep_ref = DependencyReference(
                repo_url="owner/repo",
                host="ghes.company.com",
            )

            mock_response_200 = Mock()
            mock_response_200.status_code = 200
            mock_response_200.content = b"enterprise content"
            mock_response_200.raise_for_status = Mock()

            with patch.object(
                downloader, "_resilient_get", return_value=mock_response_200
            ) as mock_get:
                result = downloader._download_github_file(dep_ref, "SKILL.md", "main")
                assert result == b"enterprise content"

                actual_headers = mock_get.call_args[1].get("headers") or mock_get.call_args[0][1]
                # Global PAT is now used for non-default hosts too
                assert actual_headers.get("Authorization") == "token default-host-pat"

            # Credential fill is not reached because the global env var is found first
            mock_cred.assert_not_called()

    def test_error_message_mentions_gh_auth_login(self):
        """Error message should mention 'gh auth login' when no token is available."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
        ):
            downloader = GitHubPackageDownloader()
            assert downloader.github_token is None

            dep_ref = DependencyReference.parse("owner/private-repo")

            mock_response_401 = Mock()
            mock_response_401.status_code = 401
            mock_response_401.raise_for_status = Mock(
                side_effect=requests_lib.exceptions.HTTPError(response=mock_response_401)
            )

            with patch.object(downloader, "_resilient_get", return_value=mock_response_401):
                with pytest.raises(RuntimeError, match="gh auth login"):
                    downloader._download_github_file(dep_ref, "SKILL.md", "main")

    def test_gh_token_env_var_used_for_modules(self):
        """GH_TOKEN should be used when no GITHUB_APM_PAT or GITHUB_TOKEN is set."""
        with patch.dict(os.environ, {"GH_TOKEN": "gh-cli-token"}, clear=True):
            downloader = GitHubPackageDownloader()
            assert downloader.github_token == "gh-cli-token"
            assert downloader._github_token_from_credential_fill is False


class TestRawContentCDNDownload:
    """Tests for CDN-first (raw.githubusercontent.com) download strategy."""

    def test_raw_cdn_used_for_github_com_without_token(self):
        """Unauthenticated github.com requests should try raw.githubusercontent.com first."""
        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("owner/repo/agents/bot.agent.md")

            mock_raw_response = Mock()
            mock_raw_response.status_code = 200
            mock_raw_response.content = b"# Agent content"

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                mock_get.return_value = mock_raw_response

                result = downloader._download_github_file(dep_ref, "agents/bot.agent.md", "main")

            assert result == b"# Agent content"
            # Should have hit raw.githubusercontent.com, not API
            call_url = mock_get.call_args[0][0]
            assert call_url.startswith("https://raw.githubusercontent.com/")
            assert not call_url.startswith("https://api.github.com/")

    def test_raw_cdn_not_used_when_token_present(self):
        """Authenticated requests should go straight to Contents API."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "my-token"}, clear=True):
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("owner/repo/agents/bot.agent.md")

            mock_api_response = Mock()
            mock_api_response.status_code = 200
            mock_api_response.headers = {"X-RateLimit-Remaining": "4999"}
            mock_api_response.content = b"# Agent content"
            mock_api_response.raise_for_status = Mock()

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                mock_get.return_value = mock_api_response

                result = downloader._download_github_file(dep_ref, "agents/bot.agent.md", "main")

            assert result == b"# Agent content"
            # Should use API with auth, not raw CDN
            call_url = mock_get.call_args[0][0]
            assert call_url.startswith("https://api.github.com/")

    def test_raw_cdn_not_used_for_enterprise_host(self):
        """Enterprise hosts should use API directly (no raw.githubusercontent.com)."""
        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("owner/repo/agents/bot.agent.md")
            dep_ref.host = "github.mycompany.com"

            mock_api_response = Mock()
            mock_api_response.status_code = 200
            mock_api_response.headers = {}
            mock_api_response.content = b"# Agent content"
            mock_api_response.raise_for_status = Mock()

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                mock_get.return_value = mock_api_response

                result = downloader._download_github_file(dep_ref, "agents/bot.agent.md", "main")

            assert result == b"# Agent content"
            call_url = mock_get.call_args[0][0]
            assert not call_url.startswith("https://raw.githubusercontent.com/")
            assert call_url.startswith("https://github.mycompany.com/")

    def test_raw_cdn_fallback_to_api_on_404(self):
        """If raw CDN returns 404, should fall through to the API path."""
        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("owner/private-repo/agents/bot.agent.md")

            # Raw CDN returns 404 (private repo or file doesn't exist)
            mock_raw_404 = Mock()
            mock_raw_404.status_code = 404
            mock_raw_404.content = b"404: Not Found"

            # API also returns 404 with proper error handling
            mock_api_404 = Mock()
            mock_api_404.status_code = 404
            mock_api_404.headers = {}
            mock_api_404.raise_for_status = Mock(
                side_effect=requests_lib.exceptions.HTTPError(response=mock_api_404)
            )

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                # First 2 calls: raw CDN (main + master fallback) → 404
                # Third call: API → 404
                mock_get.side_effect = [mock_raw_404, mock_raw_404, mock_api_404, mock_api_404]

                with pytest.raises(RuntimeError, match="File not found"):
                    downloader._download_github_file(dep_ref, "agents/bot.agent.md", "main")

    def test_raw_cdn_fallback_main_to_master(self):
        """If raw CDN 404s on 'main', should try 'master' before API fallback."""
        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("owner/repo/agents/bot.agent.md")

            # raw CDN: main → 404, master → 200
            mock_raw_404 = Mock()
            mock_raw_404.status_code = 404

            mock_raw_200 = Mock()
            mock_raw_200.status_code = 200
            mock_raw_200.content = b"# Found on master"

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                mock_get.side_effect = [mock_raw_404, mock_raw_200]

                result = downloader._download_github_file(dep_ref, "agents/bot.agent.md", "main")

            assert result == b"# Found on master"
            # Second call should be to master
            assert mock_get.call_count == 2
            second_url = mock_get.call_args_list[1][0][0]
            assert "/master/" in second_url

    def test_raw_cdn_network_error_falls_through(self):
        """If raw CDN raises a network error, should fall through to API."""
        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("owner/repo/agents/bot.agent.md")

            mock_api_response = Mock()
            mock_api_response.status_code = 200
            mock_api_response.headers = {}
            mock_api_response.content = b"# From API"
            mock_api_response.raise_for_status = Mock()

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                # Raw CDN: network error; API: success
                mock_get.side_effect = [
                    requests_lib.exceptions.ConnectionError("CDN unreachable"),
                    mock_api_response,
                ]

                result = downloader._download_github_file(dep_ref, "agents/bot.agent.md", "v1.0.0")

            assert result == b"# From API"

    def test_raw_cdn_no_branch_fallback_for_specific_ref(self):
        """For a specific ref (not main/master), raw CDN should not try branch fallback."""
        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference.parse("owner/repo/agents/bot.agent.md")

            mock_raw_404 = Mock()
            mock_raw_404.status_code = 404

            mock_api_404 = Mock()
            mock_api_404.status_code = 404
            mock_api_404.headers = {}
            mock_api_404.raise_for_status = Mock(
                side_effect=requests_lib.exceptions.HTTPError(response=mock_api_404)
            )

            with patch("apm_cli.deps.github_downloader.requests.get") as mock_get:
                # Raw CDN: 404, then API: 404 with specific ref error
                mock_get.side_effect = [mock_raw_404, mock_api_404]

                with pytest.raises(RuntimeError, match="File not found.*at ref 'v2.0.0'"):  # noqa: RUF043
                    downloader._download_github_file(dep_ref, "agents/bot.agent.md", "v2.0.0")

            # Should be exactly 2 calls: 1 raw CDN (no master fallback) + 1 API
            assert mock_get.call_count == 2

    def test_try_raw_download_returns_none_on_404(self):
        """_try_raw_download should return None on 404."""
        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            downloader = GitHubPackageDownloader()

            mock_response = Mock()
            mock_response.status_code = 404

            with patch("apm_cli.deps.github_downloader.requests.get", return_value=mock_response):
                result = downloader._try_raw_download("owner", "repo", "main", "file.md")

            assert result is None

    def test_try_raw_download_returns_content_on_200(self):
        """_try_raw_download should return bytes on success."""
        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            downloader = GitHubPackageDownloader()

            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.content = b"hello world"

            with patch("apm_cli.deps.github_downloader.requests.get", return_value=mock_response):
                result = downloader._try_raw_download("owner", "repo", "main", "file.md")

            assert result == b"hello world"


class TestVirtualFilePackageYamlGeneration:
    """Tests that apm.yml for virtual packages is always valid YAML."""

    def _make_dep_ref(self, virtual_path):
        """Helper: build a minimal DependencyReference for a virtual file."""
        from apm_cli.models.apm_package import DependencyReference

        dep_ref = Mock(spec=DependencyReference)
        dep_ref.is_virtual = True
        dep_ref.virtual_path = virtual_path
        dep_ref.reference = "main"
        dep_ref.repo_url = "github/awesome-copilot"
        dep_ref.get_virtual_package_name.return_value = "awesome-copilot-swe-subagent"
        dep_ref.to_github_url.return_value = (
            f"https://github.com/github/awesome-copilot/blob/main/{virtual_path}"
        )
        dep_ref.is_virtual_file.return_value = True
        dep_ref.VIRTUAL_FILE_EXTENSIONS = [
            ".prompt.md",
            ".instructions.md",
            ".chatmode.md",
            ".agent.md",
        ]
        return dep_ref

    def _make_collection_dep_ref(self, virtual_path):
        """Helper: build a minimal DependencyReference for a virtual collection."""
        from apm_cli.models.apm_package import DependencyReference

        dep_ref = Mock(spec=DependencyReference)
        dep_ref.is_virtual = True
        dep_ref.virtual_path = virtual_path
        dep_ref.reference = "main"
        dep_ref.repo_url = "github/my-org"
        dep_ref.get_virtual_package_name.return_value = "my-org-my-collection"
        dep_ref.to_github_url.return_value = (
            f"https://github.com/github/my-org/blob/main/{virtual_path}"
        )
        dep_ref.is_virtual_collection.return_value = True
        return dep_ref

    def test_yaml_with_colon_in_description(self, tmp_path):
        """apm.yml must be valid when the agent description contains a colon."""
        import yaml

        agent_content = (
            b"---\n"
            b"name: 'SWE'\n"
            b"description: 'Senior software engineer subagent for implementation tasks:"
            b" feature development, debugging, refactoring, and testing.'\n"
            b"tools: ['vscode']\n"
            b"---\n\n## Body\n"
        )

        dep_ref = self._make_dep_ref("agents/swe-subagent.agent.md")
        target_path = tmp_path / "pkg"

        downloader = GitHubPackageDownloader()
        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            with patch.object(downloader, "download_raw_file", return_value=agent_content):
                downloader.download_virtual_file_package(dep_ref, target_path)

        apm_yml_path = target_path / "apm.yml"
        assert apm_yml_path.exists(), "apm.yml was not created"

        content = apm_yml_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)  # must not raise

        expected = (
            "Senior software engineer subagent for implementation tasks:"
            " feature development, debugging, refactoring, and testing."
        )
        assert parsed["description"] == expected

    def test_yaml_with_colon_in_name(self, tmp_path):
        """apm.yml must be valid even when the package name contains a colon."""
        import yaml

        dep_ref = self._make_dep_ref("agents/my-agent.agent.md")
        dep_ref.get_virtual_package_name.return_value = "org-name: special"

        agent_content = b"---\nname: 'plain'\ndescription: 'plain'\n---\n"
        target_path = tmp_path / "pkg"

        downloader = GitHubPackageDownloader()
        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            with patch.object(downloader, "download_raw_file", return_value=agent_content):
                downloader.download_virtual_file_package(dep_ref, target_path)

        content = (target_path / "apm.yml").read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        assert parsed["name"] == "org-name: special"

    def test_yaml_without_special_characters_still_valid(self, tmp_path):
        """apm.yml generation must still work for ordinary descriptions."""
        import yaml

        agent_content = (
            b"---\nname: 'Simple Agent'\ndescription: 'A simple agent without special chars'\n---\n"
        )

        dep_ref = self._make_dep_ref("agents/simple.agent.md")
        target_path = tmp_path / "pkg"

        downloader = GitHubPackageDownloader()
        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            with patch.object(downloader, "download_raw_file", return_value=agent_content):
                downloader.download_virtual_file_package(dep_ref, target_path)

        content = (target_path / "apm.yml").read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        assert parsed["description"] == "A simple agent without special chars"

    def test_collection_yaml_with_colon_in_description(self, tmp_path):
        """apm.yml for collection packages must be valid when description contains a colon."""
        import yaml

        # A minimal .collection.yml whose description contains ":"
        collection_manifest = (
            b"id: my-collection\n"
            b"name: My Collection\n"
            b"description: 'A collection for tasks: feature development, debugging.'\n"
            b"items:\n"
            b"  - path: agents/my-agent.agent.md\n"
            b"    kind: agent\n"
        )
        agent_file = b"---\nname: My Agent\n---\n## Body\n"

        dep_ref = self._make_collection_dep_ref("collections/my-collection")
        target_path = tmp_path / "pkg"

        downloader = GitHubPackageDownloader()

        def _fake_download(dep_ref_arg, path, ref):
            if "collection" in path:
                return collection_manifest
            return agent_file

        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            with patch.object(downloader, "download_raw_file", side_effect=_fake_download):
                downloader.download_collection_package(dep_ref, target_path)

        content = (target_path / "apm.yml").read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)  # must not raise

        assert parsed["description"] == "A collection for tasks: feature development, debugging."

    def test_collection_yaml_with_colon_in_tags(self, tmp_path):
        """apm.yml for collection packages must be valid when tags contain a colon."""
        import yaml

        collection_manifest = (
            b"id: tagged-collection\n"
            b"name: Tagged\n"
            b"description: Normal description\n"
            b"tags:\n"
            b"  - 'scope: engineering'\n"
            b"  - plain-tag\n"
            b"items:\n"
            b"  - path: agents/my-agent.agent.md\n"
            b"    kind: agent\n"
        )
        agent_file = b"---\nname: My Agent\n---\n## Body\n"

        dep_ref = self._make_collection_dep_ref("collections/tagged-collection")
        target_path = tmp_path / "pkg"

        downloader = GitHubPackageDownloader()

        def _fake_download(dep_ref_arg, path, ref):
            if "collection" in path:
                return collection_manifest
            return agent_file

        with patch.dict(os.environ, {}, clear=True), _CRED_FILL_PATCH:
            with patch.object(downloader, "download_raw_file", side_effect=_fake_download):
                downloader.download_collection_package(dep_ref, target_path)

        content = (target_path / "apm.yml").read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)

        assert parsed["tags"] == ["scope: engineering", "plain-tag"]


if __name__ == "__main__":
    pytest.main([__file__])
