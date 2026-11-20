"""Phase-3 unit tests for ``apm_cli.deps.github_downloader_validation``.

Covers branches not hit by the existing test_github_downloader_validation.py:

* ``_is_sha_pin`` true and false cases
* ``validate_virtual_package_exists`` non-virtual dep raises ValueError
* ``validate_virtual_package_exists`` is_virtual_file probe path
* ``validate_virtual_package_exists`` warn_callback fires on git-fallback
* ``_directory_exists_at_ref`` ADO / non-GitHub skips probe
* ``_directory_exists_at_ref`` github.com 200, 404, non-200
* ``_directory_exists_at_ref`` request exception
* ``_directory_exists_at_ref`` ghe.com URL shape
* ``_directory_exists_at_ref`` GHE non-ghe.com GHES URL
* ``_build_validation_attempts`` artifactory returns empty list
* ``_build_validation_attempts`` ADO basic encodes PAT as HTTP Basic
* ``_build_validation_attempts`` ADO bearer uses Bearer header
* ``_build_validation_attempts`` non-ADO token uses Bearer header
* ``_ref_exists_via_ls_remote`` no attempts returns (False, None)
* ``_ref_exists_via_ls_remote`` SHA-pin scan match
* ``_ref_exists_via_ls_remote`` tag match
* ``_ref_exists_via_ls_remote`` all attempts fail
* ``_path_exists_in_tree_at_ref`` fetch failure returns False
* ``_path_exists_in_tree_at_ref`` ls-tree failure returns False
* ``_path_exists_in_tree_at_ref`` empty ls-tree output returns False
* ``_path_exists_in_tree_at_ref`` success returns True
* ``_ssh_attempt_allowed`` returns False when SSH not preferred
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.github_downloader_validation import (
    _build_validation_attempts,
    _directory_exists_at_ref,
    validate_virtual_package_exists,
)
from apm_cli.deps.github_rate_limit import GitHubThrottle, GitHubThrottleError
from apm_cli.models.apm_package import DependencyReference

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_downloader(token: str = "tok", host: str = "github.com") -> GitHubPackageDownloader:  # noqa: S107
    """Return a downloader with a mocked auth_resolver."""
    dl = GitHubPackageDownloader()
    dl.github_host = host
    resolver = MagicMock()
    dep_ctx = MagicMock()
    dep_ctx.token = token
    dep_ctx.auth_scheme = "basic"
    resolver.resolve_for_dep.return_value = dep_ctx
    resolver.git_env_for_context.side_effect = lambda auth_ctx, *, base_env: {
        **base_env,
        **auth_ctx.git_env,
    }
    resolver._build_git_env.side_effect = AuthResolver._build_git_env
    resolver.classify_host.return_value = MagicMock(
        kind="github", api_base="https://api.github.com"
    )
    dl.auth_resolver = resolver
    return dl


def _make_github_dep(
    repo_url: str = "owner/repo",
    host: str | None = None,
    ref: str | None = "main",
    vpath: str = "skills/my-skill",
    is_virtual: bool = True,
    is_file: bool = False,
) -> MagicMock:
    dep = MagicMock(spec=DependencyReference)
    dep.repo_url = repo_url
    dep.host = host
    dep.port = None
    dep.reference = ref
    dep.virtual_path = vpath
    dep.is_virtual = is_virtual
    dep.is_virtual_file.return_value = is_file
    dep.is_virtual_subdirectory.return_value = not is_file and is_virtual
    dep.is_azure_devops.return_value = False
    dep.is_artifactory.return_value = False
    dep.is_insecure = False
    dep.explicit_scheme = None
    return dep


# ---------------------------------------------------------------------------
# _is_sha_pin
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# validate_virtual_package_exists: non-virtual raises ValueError
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# validate_virtual_package_exists: virtual file probe
# ---------------------------------------------------------------------------


class TestValidateVirtualFile:
    def test_virtual_file_probe_propagates_confirmed_throttle(self) -> None:
        """A typed throttle is indeterminate, not a virtual-path false negative."""
        dl = _make_downloader()
        dep = _make_github_dep(is_file=True)
        throttle = GitHubThrottleError(GitHubThrottle(429, "http-429"), "github.com")

        with patch.object(dl, "download_raw_file", side_effect=throttle):
            with pytest.raises(GitHubThrottleError) as exc_info:
                validate_virtual_package_exists(dl, dep)

        assert exc_info.value is throttle


# ---------------------------------------------------------------------------
# validate_virtual_package_exists: warn_callback fired on git-fallback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _directory_exists_at_ref
# ---------------------------------------------------------------------------


class TestDirectoryExistsAtRef:
    def _log(self, _msg: str) -> None:
        pass

    def test_request_exception_returns_false(self) -> None:
        import requests

        dl = _make_downloader()
        dep = _make_github_dep(host=None)
        dep.is_azure_devops.return_value = False

        with patch.object(
            dl, "_resilient_get", side_effect=requests.exceptions.ConnectionError("err")
        ):
            result = _directory_exists_at_ref(dl, dep, "path", "main", self._log)

        assert result is False


# ---------------------------------------------------------------------------
# _build_validation_attempts
# ---------------------------------------------------------------------------


class TestBuildValidationAttempts:
    def _make_dep(self) -> MagicMock:
        dep = _make_github_dep()
        dep.is_artifactory.return_value = False
        dep.host = "github.com"
        dep.port = None
        return dep

    def test_github_token_delegates_header_format_to_auth_resolver(self) -> None:
        dl = _make_downloader(token="ghp_token")
        dl.auth_resolver.resolve_for_dep.return_value.token = "ghp_token"
        dl.auth_resolver.resolve_for_dep.return_value.auth_scheme = "basic"
        dl.auth_resolver.git_env_for_remote.return_value = {"CANONICAL": "github"}
        dl.auth_resolver.build_native_git_credential_env.return_value = {"NATIVE_HELPER": "github"}
        dep = self._make_dep()
        dep.is_azure_devops.return_value = False
        dl.auth_resolver.classify_host.return_value = MagicMock(kind="github")

        attempts = _build_validation_attempts(dl, dep, lambda m: None)
        auth_attempts = [a for a in attempts if "header" in a.label.lower()]
        assert len(auth_attempts) >= 1
        assert auth_attempts[0].env == {"CANONICAL": "github"}
        dl.auth_resolver.git_env_for_remote.assert_any_call(
            dl.auth_resolver.resolve_for_dep.return_value,
            "https://github.com/owner/repo.git",
        )
        assert attempts[1].env == {"NATIVE_HELPER": "github"}
        dl.auth_resolver.build_native_git_credential_env.assert_called_once_with(
            dl.auth_resolver.classify_host.return_value,
            "https://github.com/owner/repo.git",
        )


# ---------------------------------------------------------------------------
# _ref_exists_via_ls_remote
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _path_exists_in_tree_at_ref
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _ssh_attempt_allowed
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _split_owner_repo edge cases
# ---------------------------------------------------------------------------
