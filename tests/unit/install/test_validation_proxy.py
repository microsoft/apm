"""Tests for proxy validation bypass fix (PROXY_REGISTRY_ONLY guard).

PR #615 adds `is_enforce_only()` guards to skip GitHub API calls when
PROXY_REGISTRY_ONLY=1 is set:

1. Guard 1 (~line 199): Virtual package path -- skips validate_virtual_package_exists
2. Guard 2 (~line 514): GitHub.com path -- skips _check_repo API call
3. Guard 3 (~line 609): Parse-failure fallback path -- skips _check_repo_fallback API call
4. Guard 4 (~line 273): ADO/GHES path -- skips git ls-remote probe

This test suite validates that when PROXY_REGISTRY_ONLY=1, API calls are
skipped and the function returns True (allowing the download step to enforce
proxy access and surface a proxy 404 if needed).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from apm_cli.install import validation


class TestProxyBypassGuard:
    """Test that is_enforce_only() guards skip network calls on all code paths."""

    def _setup_resolver(self):
        """Build a minimal AuthResolver mock for GitHub.com packages."""
        resolver = MagicMock()
        host_info = MagicMock()
        host_info.api_base = "https://api.github.com"
        host_info.display_name = "github.com"
        host_info.kind = "github"
        host_info.has_public_repos = True
        resolver.classify_host.return_value = host_info
        ctx = MagicMock(source="env", token_type="pat", token=None)
        resolver.resolve.return_value = ctx
        resolver.resolve_for_dep.return_value = ctx

        # Single-call shim: invoke the operation once unauth.
        def _fake_fallback(host, op, **kwargs):
            return op(None, {})

        resolver.try_with_fallback.side_effect = _fake_fallback
        return resolver

    def test_github_path_skipped_when_enforce_only(self):
        """Guard 1: GitHub.com path skips API when PROXY_REGISTRY_ONLY=1."""
        resolver = self._setup_resolver()

        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with patch("apm_cli.install.validation.requests.get") as mock_get:
                result = validation._validate_package_exists(
                    "microsoft/apm",
                    verbose=False,
                    auth_resolver=resolver,
                    logger=None,
                )

        # Should return True (proxy-only mode: download step handles enforcement)
        assert result is True

        # API call must be skipped entirely
        assert mock_get.call_count == 0

    def test_github_path_calls_api_without_enforce_only(self):
        """GitHub.com path calls API when PROXY_REGISTRY_ONLY is NOT set."""
        resolver = self._setup_resolver()

        # Explicitly clear both PROXY_REGISTRY_ONLY and the deprecated ARTIFACTORY_ONLY
        # alias to ensure API path is taken regardless of test environment state.
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PROXY_REGISTRY_ONLY", "ARTIFACTORY_ONLY")
        }

        with patch.dict(os.environ, env, clear=True):
            with patch("apm_cli.install.validation.requests.get") as mock_get:
                # Mock a successful API response (200 OK).
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_get.return_value = mock_response

                result = validation._validate_package_exists(
                    "microsoft/apm",
                    verbose=False,
                    auth_resolver=resolver,
                    logger=None,
                )

        # Should return True (API succeeded).
        assert result is True

        # API call must have been made (to probe the repo).
        assert mock_get.call_count >= 1

    def test_fallback_path_skipped_when_enforce_only(self):
        """Guard 2: Fallback path skips API when PROXY_REGISTRY_ONLY=1.

        The fallback path is triggered when DependencyReference.parse() raises
        an error but the input matches owner/repo format. We mock the parse
        failure to exercise this code path.
        """
        resolver = self._setup_resolver()

        # Mock DependencyReference.parse to raise, triggering the fallback path.
        # The fallback will extract owner/repo from the package string directly.
        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with patch(
                "apm_cli.models.apm_package.DependencyReference.parse",
                side_effect=ValueError("Simulated parse error"),
            ):
                with patch("apm_cli.install.validation.requests.get") as mock_get:
                    # Use a valid owner/repo format for the fallback path.
                    result = validation._validate_package_exists(
                        "owner/repo",
                        verbose=False,
                        auth_resolver=resolver,
                        logger=None,
                    )

        # Guard 2 should return True (proxy-only mode).
        assert result is True

        # API call must be skipped.
        assert mock_get.call_count == 0

    def test_fallback_path_calls_api_without_enforce_only(self):
        """Fallback path calls API when PROXY_REGISTRY_ONLY is NOT set.

        Same setup as above, but with PROXY_REGISTRY_ONLY unset, the fallback
        should proceed to make an API call.
        """
        resolver = self._setup_resolver()

        # Explicitly clear both PROXY_REGISTRY_ONLY and the deprecated ARTIFACTORY_ONLY
        # alias to ensure API path is taken regardless of test environment state.
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PROXY_REGISTRY_ONLY", "ARTIFACTORY_ONLY")
        }

        with patch.dict(os.environ, env, clear=True):
            with patch(
                "apm_cli.models.apm_package.DependencyReference.parse",
                side_effect=ValueError("Simulated parse error"),
            ):
                with patch("apm_cli.install.validation.requests.get") as mock_get:
                    # Mock a successful API response.
                    mock_response = MagicMock()
                    mock_response.status_code = 200
                    mock_get.return_value = mock_response

                    # Use a valid owner/repo format for the fallback path.
                    result = validation._validate_package_exists(
                        "owner/repo",
                        verbose=False,
                        auth_resolver=resolver,
                        logger=None,
                    )

        # Should return True (API succeeded).
        assert result is True

        # API call must have been made.
        assert mock_get.call_count >= 1

    def test_fallback_path_rejects_invalid_owner_repo_format(self):
        """Fallback path rejects inputs that don't match owner/repo format.

        Even in the fallback path, we have a strict regex check to prevent
        path-confusion attacks. Inputs like "../", embedded slashes, or control
        bytes should be rejected outright.
        """
        resolver = self._setup_resolver()

        # Mock DependencyReference.parse to raise.
        with patch(
            "apm_cli.models.apm_package.DependencyReference.parse",
            side_effect=ValueError("Simulated parse error"),
        ):
            with patch("apm_cli.install.validation.requests.get") as mock_get:
                # Invalid formats should be rejected (return False) WITHOUT
                # making any API call.
                test_cases = [
                    "../../../evil",  # Path traversal
                    "owner/repo/extra",  # Too many segments
                    "owner",  # No slash
                    "owner/",  # Missing repo
                    "/repo",  # Missing owner
                ]

                for invalid_input in test_cases:
                    mock_get.reset_mock()
                    result = validation._validate_package_exists(
                        invalid_input,
                        verbose=False,
                        auth_resolver=resolver,
                        logger=None,
                    )

                    # Should be rejected without an API call.
                    assert result is False, f"Expected False for {invalid_input}"
                    assert mock_get.call_count == 0, f"Unexpected API call for {invalid_input}"

    def test_enforce_only_variants(self):
        """is_enforce_only() recognizes multiple truthy env var values.

        The function accepts "1", "true", "yes" (case-insensitive).
        """
        resolver = self._setup_resolver()

        # Test multiple truthy variants.
        truthy_values = ["1", "true", "True", "TRUE", "yes", "YES"]

        for value in truthy_values:
            with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": value}):
                with patch("apm_cli.install.validation.requests.get") as mock_get:
                    result = validation._validate_package_exists(
                        "microsoft/apm",
                        verbose=False,
                        auth_resolver=resolver,
                        logger=None,
                    )

                    # Each variant should skip the API call.
                    assert mock_get.call_count == 0, (
                        f"API was called with PROXY_REGISTRY_ONLY={value}"
                    )
                    assert result is True

    def test_virtual_path_skipped_when_enforce_only(self):
        """Guard 3: Virtual package path skips downloader when PROXY_REGISTRY_ONLY=1.

        When a dep_ref has is_virtual=True and is not a subdirectory-on-
        non-GitHub-host, the validator would normally call
        GitHubPackageDownloader.validate_virtual_package_exists(). The new
        is_enforce_only() guard at line 199-200 skips that call and returns True.
        """
        resolver = self._setup_resolver()

        # Create a virtual dep_ref mock (simulating a virtual package).
        virtual_ref = MagicMock()
        virtual_ref.is_virtual = True
        virtual_ref.virtual_path = "prompts/code-review.prompt.md"
        virtual_ref.is_virtual_subdirectory.return_value = False  # Not a subdirectory
        virtual_ref.is_local = False  # Not local
        virtual_ref.local_path = None
        virtual_ref.repo_url = "owner/repo"
        virtual_ref.host = "github.com"
        virtual_ref.reference = "main"
        virtual_ref.is_azure_devops.return_value = False

        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with patch("apm_cli.utils.github_host.is_github_hostname") as mock_is_github:
                mock_is_github.return_value = True  # github.com is GitHub
                with patch(
                    "apm_cli.deps.github_downloader.GitHubPackageDownloader"
                ) as mock_downloader_class:
                    # The mock downloader instance's validate_virtual_package_exists should NOT be called.
                    mock_downloader_instance = MagicMock()
                    mock_downloader_class.return_value = mock_downloader_instance

                    result = validation._validate_package_exists(
                        "owner/repo/prompts/code-review.prompt.md",
                        verbose=False,
                        auth_resolver=resolver,
                        logger=None,
                        dep_ref=virtual_ref,
                    )

        # Guard 3 should return True (proxy-only mode).
        assert result is True

        # validate_virtual_package_exists must NOT be called.
        mock_downloader_instance.validate_virtual_package_exists.assert_not_called()

    def test_virtual_path_calls_downloader_without_enforce_only(self):
        """Virtual package path calls downloader when PROXY_REGISTRY_ONLY is NOT set.

        Same setup as above, but without PROXY_REGISTRY_ONLY, the virtual
        package validation should proceed to call validate_virtual_package_exists.
        """
        resolver = self._setup_resolver()

        # Create a virtual dep_ref mock (simulating a virtual package).
        virtual_ref = MagicMock()
        virtual_ref.is_virtual = True
        virtual_ref.virtual_path = "src/agent"
        # B2 fix: is_virtual_subdirectory=False is consistent with is_github_hostname=True
        # (GitHub hosts never use the subdirectory probe path). Setting it True alongside
        # is_github_hostname=True previously created a contradictory state where
        # virtual_subdir_repo_probe evaluated to False anyway, making the subdirectory
        # flag irrelevant and the test misleading.
        virtual_ref.is_virtual_subdirectory.return_value = False
        virtual_ref.is_local = False  # Not local
        virtual_ref.local_path = None
        virtual_ref.repo_url = "owner/repo"
        virtual_ref.host = "github.com"
        virtual_ref.reference = "main"
        virtual_ref.is_azure_devops.return_value = False

        # Explicitly clear both PROXY_REGISTRY_ONLY and the deprecated ARTIFACTORY_ONLY
        # alias to ensure the downloader path is taken regardless of test environment state.
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PROXY_REGISTRY_ONLY", "ARTIFACTORY_ONLY")
        }

        with patch.dict(os.environ, env, clear=True):
            with patch("apm_cli.utils.github_host.is_github_hostname") as mock_is_github:
                mock_is_github.return_value = True  # github.com is GitHub
                with patch(
                    "apm_cli.deps.github_downloader.GitHubPackageDownloader"
                ) as mock_downloader_class:
                    # Mock the downloader instance and its validate_virtual_package_exists method.
                    mock_downloader_instance = MagicMock()
                    mock_downloader_instance.validate_virtual_package_exists.return_value = True
                    mock_downloader_class.return_value = mock_downloader_instance

                    result = validation._validate_package_exists(
                        "owner/repo/src/agent",
                        verbose=False,
                        auth_resolver=resolver,
                        logger=None,
                        dep_ref=virtual_ref,
                    )

        # Should return True (downloader validation succeeded).
        assert result is True

        # validate_virtual_package_exists MUST be called.
        mock_downloader_instance.validate_virtual_package_exists.assert_called_once()

    def test_ado_ghes_path_skipped_when_enforce_only(self):
        """Guard 4: ADO/GHES git ls-remote path skips probe when PROXY_REGISTRY_ONLY=1.

        When a dep has a non-github.com host the validator would normally run
        ``git ls-remote`` against ADO or GHES. The new is_enforce_only() guard
        skips that subprocess call and returns True immediately.
        """
        resolver = self._setup_resolver()

        ado_ref = MagicMock()
        ado_ref.is_virtual = False
        ado_ref.is_local = False
        ado_ref.local_path = None
        ado_ref.host = "dev.azure.com"
        ado_ref.repo_url = "myorg/myrepo"
        ado_ref.port = None
        ado_ref.is_azure_devops.return_value = True
        ado_ref.is_virtual_subdirectory.return_value = False
        ado_ref.explicit_scheme = None
        ado_ref.is_insecure = False

        with patch.dict(os.environ, {"PROXY_REGISTRY_ONLY": "1"}):
            with patch("subprocess.run") as mock_run:
                result = validation._validate_package_exists(
                    "myorg/myrepo",
                    verbose=False,
                    auth_resolver=resolver,
                    logger=None,
                    dep_ref=ado_ref,
                )

        # Guard 4 should return True (proxy-only mode).
        assert result is True

        # git ls-remote must NOT be called.
        assert mock_run.call_count == 0, (
            "subprocess.run (git ls-remote) was called in proxy-only mode"
        )

    def test_ado_ghes_path_calls_git_without_enforce_only(self):
        """ADO/GHES path runs git ls-remote when PROXY_REGISTRY_ONLY is NOT set.

        Patches GitHubPackageDownloader at its source module so the per-function
        ``from apm_cli.deps.github_downloader import GitHubPackageDownloader``
        picks up the mock, avoiding real auth/URL-building internals while still
        exercising the subprocess.run call site.
        """
        resolver = self._setup_resolver()

        ado_ref = MagicMock()
        ado_ref.is_virtual = False
        ado_ref.is_local = False
        ado_ref.local_path = None
        ado_ref.host = "dev.azure.com"
        ado_ref.repo_url = "myorg/myrepo"
        ado_ref.port = None
        ado_ref.is_azure_devops.return_value = True
        ado_ref.is_virtual_subdirectory.return_value = False
        ado_ref.explicit_scheme = None
        ado_ref.is_insecure = False

        # Stub the downloader so _build_repo_url and .git_env don't hit real code.
        mock_downloader = MagicMock()
        mock_downloader._build_repo_url.return_value = "https://dev.azure.com/myorg/myrepo"
        mock_downloader.git_env = {}
        mock_downloader._sanitize_git_error.return_value = ""
        mock_dl_cls = MagicMock(return_value=mock_downloader)

        # Explicitly clear both enforce env vars.
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PROXY_REGISTRY_ONLY", "ARTIFACTORY_ONLY")
        }

        with patch.dict(os.environ, env, clear=True):
            with patch("apm_cli.deps.github_downloader.GitHubPackageDownloader", mock_dl_cls):
                with patch("subprocess.run") as mock_run:
                    mock_proc = MagicMock()
                    mock_proc.returncode = 0
                    mock_proc.stderr = ""
                    mock_run.return_value = mock_proc

                    result = validation._validate_package_exists(
                        "myorg/myrepo",
                        verbose=False,
                        auth_resolver=resolver,
                        logger=None,
                        dep_ref=ado_ref,
                    )

        # Should return True (git ls-remote succeeded).
        assert result is True

        # subprocess.run (git ls-remote) MUST have been called.
        assert mock_run.call_count >= 1, "subprocess.run (git ls-remote) was not called"

    def test_deprecated_artifactory_only_skips_api(self):
        """ARTIFACTORY_ONLY (deprecated alias) also triggers proxy-only bypass.

        is_enforce_only() falls back to ARTIFACTORY_ONLY when PROXY_REGISTRY_ONLY
        is unset. This test verifies the deprecated alias is honoured so existing
        deployments that have not yet migrated keep working correctly.
        """
        resolver = self._setup_resolver()

        # Set the deprecated alias and ensure the canonical var is absent.
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PROXY_REGISTRY_ONLY", "ARTIFACTORY_ONLY")
        }
        env["ARTIFACTORY_ONLY"] = "1"

        with patch.dict(os.environ, env, clear=True):
            with patch("apm_cli.install.validation.requests.get") as mock_get:
                result = validation._validate_package_exists(
                    "microsoft/apm",
                    verbose=False,
                    auth_resolver=resolver,
                    logger=None,
                )

        # The deprecated alias must trigger the same bypass.
        assert result is True
        assert mock_get.call_count == 0, "API was called despite ARTIFACTORY_ONLY=1"
