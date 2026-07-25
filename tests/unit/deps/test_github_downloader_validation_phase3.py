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

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps import github_downloader_validation as gdv
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.github_downloader_validation import (
    AttemptSpec,
    _build_validation_attempts,
    _directory_exists_at_ref,
    _is_sha_pin,
    _path_exists_in_tree_at_ref,
    _ref_exists_via_ls_remote,
    _split_owner_repo,
    _ssh_attempt_allowed,
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


class TestIsShaPin:
    @pytest.mark.parametrize(
        "ref",
        ["abc1234", "abc1234def5678", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"],
    )
    def test_sha_like_refs_return_true(self, ref: str) -> None:
        assert _is_sha_pin(ref) is True

    @pytest.mark.parametrize("ref", ["main", "v1.0.0", "feature/branch", "HEAD"])
    def test_non_sha_refs_return_false(self, ref: str) -> None:
        assert _is_sha_pin(ref) is False

    def test_too_short_returns_false(self) -> None:
        assert _is_sha_pin("abc") is False  # less than 7 hex chars

    def test_exactly_7_hex_returns_true(self) -> None:
        assert _is_sha_pin("abc1234") is True


# ---------------------------------------------------------------------------
# validate_virtual_package_exists: non-virtual raises ValueError
# ---------------------------------------------------------------------------


class TestValidateVirtualPackageExistsNonVirtual:
    def test_non_virtual_dep_raises_value_error(self) -> None:
        dl = _make_downloader()
        dep = MagicMock()
        dep.is_virtual = False
        dep.virtual_path = None
        with pytest.raises(ValueError, match="virtual"):
            validate_virtual_package_exists(dl, dep)


# ---------------------------------------------------------------------------
# validate_virtual_package_exists: virtual file probe
# ---------------------------------------------------------------------------


class TestValidateVirtualFile:
    def test_virtual_file_probe_success(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep(is_file=True)
        dep.is_virtual_subdirectory.return_value = False

        with patch.object(dl, "download_raw_file", return_value=b"data"):
            result = validate_virtual_package_exists(dl, dep)

        assert result is True

    def test_virtual_file_probe_failure(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep(is_file=True)
        dep.is_virtual_subdirectory.return_value = False

        with patch.object(dl, "download_raw_file", side_effect=RuntimeError("404")):
            result = validate_virtual_package_exists(dl, dep)

        assert result is False

    def test_virtual_file_probe_propagates_confirmed_throttle(self) -> None:
        """A typed throttle is indeterminate, not a virtual-path false negative."""
        dl = _make_downloader()
        dep = _make_github_dep(is_file=True)
        throttle = GitHubThrottleError(GitHubThrottle(429, "http-429"), "github.com")

        with patch.object(dl, "download_raw_file", side_effect=throttle):
            with pytest.raises(GitHubThrottleError) as exc_info:
                validate_virtual_package_exists(dl, dep)

        assert exc_info.value is throttle

    def test_empty_vpath_rejected_before_probe(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep(vpath="", is_file=True)
        dep.is_virtual_subdirectory.return_value = False

        with patch.object(dl, "download_raw_file") as mock_raw:
            result = validate_virtual_package_exists(dl, dep)

        assert result is False
        mock_raw.assert_not_called()


# ---------------------------------------------------------------------------
# validate_virtual_package_exists: warn_callback fired on git-fallback
# ---------------------------------------------------------------------------


class TestValidateVirtualSubdirWarnCallback:
    def test_warn_callback_fires_when_git_fallback_resolves(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep()

        warnings: list[str] = []

        with (
            patch.object(dl, "download_raw_file", side_effect=RuntimeError("404")),
            patch.object(gdv, "_directory_exists_at_ref", return_value=False),
            patch.object(
                gdv,
                "_ref_exists_via_ls_remote",
                return_value=(True, AttemptSpec("ssh", "git@github.com:owner/repo.git", {})),
            ),
            patch.object(gdv, "_path_exists_in_tree_at_ref", return_value=True),
        ):
            result = validate_virtual_package_exists(
                dl,
                dep,
                warn_callback=warnings.append,
            )

        assert result is True
        assert len(warnings) == 1
        assert "git credential fallback" in warnings[0]

    def test_no_warn_when_marker_hits_directly(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep()

        warnings: list[str] = []

        with patch.object(dl, "download_raw_file", return_value=b"data"):
            result = validate_virtual_package_exists(
                dl,
                dep,
                warn_callback=warnings.append,
            )

        assert result is True
        assert len(warnings) == 0


# ---------------------------------------------------------------------------
# _directory_exists_at_ref
# ---------------------------------------------------------------------------


class TestDirectoryExistsAtRef:
    def _log(self, _msg: str) -> None:
        pass

    def test_azure_devops_returns_false_without_probe(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep()
        dep.is_azure_devops.return_value = True

        with patch.object(dl, "_resilient_get") as mock_get:
            result = _directory_exists_at_ref(dl, dep, "path", "main", self._log)

        assert result is False
        mock_get.assert_not_called()

    def test_non_github_host_returns_false(self) -> None:
        dl = _make_downloader(host="bitbucket.example.com")
        dep = _make_github_dep(host="bitbucket.example.com")
        dep.is_azure_devops.return_value = False

        with patch.object(dl, "_resilient_get") as mock_get:
            result = _directory_exists_at_ref(dl, dep, "path", "main", self._log)

        assert result is False
        mock_get.assert_not_called()

    def test_github_com_200_returns_true(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep(host=None)
        dep.is_azure_devops.return_value = False

        resp = MagicMock()
        resp.status_code = 200
        with patch.object(dl, "_resilient_get", return_value=resp):
            result = _directory_exists_at_ref(dl, dep, "skills/foo", "main", self._log)

        assert result is True

    def test_github_com_404_returns_false(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep(host=None)
        dep.is_azure_devops.return_value = False

        resp = MagicMock()
        resp.status_code = 404
        with patch.object(dl, "_resilient_get", return_value=resp):
            result = _directory_exists_at_ref(dl, dep, "skills/foo", "main", self._log)

        assert result is False

    def test_non_200_non_404_returns_false(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep(host=None)
        dep.is_azure_devops.return_value = False

        resp = MagicMock()
        resp.status_code = 500
        with patch.object(dl, "_resilient_get", return_value=resp):
            result = _directory_exists_at_ref(dl, dep, "path", "main", self._log)

        assert result is False

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

    def test_ghe_com_uses_api_subdomain_url(self) -> None:
        """``foo.ghe.com`` -> ``https://api.foo.ghe.com/repos/...``."""
        dl = _make_downloader(host="myhost.ghe.com")
        dep = _make_github_dep(host="myhost.ghe.com")
        dep.is_azure_devops.return_value = False

        captured_urls: list[str] = []

        def _capture(url, **kwargs):
            captured_urls.append(url)
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch.object(dl, "_resilient_get", side_effect=_capture):
            _directory_exists_at_ref(dl, dep, "skills/foo", "main", self._log)

        assert len(captured_urls) == 1
        from urllib.parse import urlparse

        parsed = urlparse(captured_urls[0])
        assert parsed.hostname == "api.myhost.ghe.com"

    def test_non_ghe_non_github_host_returns_false_without_probe(self) -> None:
        """Non-github.com, non-ghe.com hosts are skipped (not GitHub-classified)."""
        dl = _make_downloader(host="corp.example.com")
        dep = _make_github_dep(host="corp.example.com")
        dep.is_azure_devops.return_value = False

        with patch.object(dl, "_resilient_get") as mock_get:
            result = _directory_exists_at_ref(dl, dep, "skills/foo", "main", self._log)

        assert result is False
        mock_get.assert_not_called()

    def test_missing_owner_repo_split_returns_false(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep(repo_url="no_slash")
        dep.is_azure_devops.return_value = False

        with patch.object(dl, "_resilient_get") as mock_get:
            result = _directory_exists_at_ref(dl, dep, "path", "main", self._log)

        assert result is False
        mock_get.assert_not_called()


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

    def test_artifactory_returns_empty(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep()
        dep.is_artifactory.return_value = True
        attempts = _build_validation_attempts(dl, dep, lambda m: None)
        assert attempts == []

    def test_no_token_skips_auth_attempt(self) -> None:
        dl = _make_downloader(token="")
        dl.auth_resolver.resolve_for_dep.return_value.token = None
        dep = self._make_dep()
        attempts = _build_validation_attempts(dl, dep, lambda m: None)
        # Only plain HTTPS attempt (no token attempt)
        labels = [a.label for a in attempts]
        assert not any("header" in lab for lab in labels)

    def test_ado_basic_uses_http_basic_header(self) -> None:
        dl = _make_downloader()
        dl.auth_resolver.resolve_for_dep.return_value.token = "myPAT"
        dl.auth_resolver.resolve_for_dep.return_value.auth_scheme = "basic"
        dep = self._make_dep()
        dep.is_azure_devops.return_value = True
        dep.host = "dev.azure.com"
        dep.repo_url = "myorg/myproject/_git/myrepo"
        dep.ado_organization = "myorg"
        dep.ado_project = "myproject"
        dep.ado_repo = "myrepo"
        dep.port = None
        dep.explicit_scheme = None
        dl.auth_resolver.classify_host.return_value = MagicMock(kind="ado")

        attempts = _build_validation_attempts(dl, dep, lambda m: None)
        auth_attempts = [
            a for a in attempts if "basic header" in a.label.lower() or "ado" in a.label.lower()
        ]
        assert len(auth_attempts) >= 1
        # Verify it's Base64 Basic, not a raw Bearer
        token_attempt = auth_attempts[0]
        env_values = list(token_attempt.env.values())
        # The GIT_CONFIG_VALUE should contain "Basic " not "Bearer "
        header_values = [
            v for v in env_values if isinstance(v, str) and ("Basic" in v or "Bearer" in v)
        ]
        assert any("Basic" in v for v in header_values)

    def test_non_ado_with_token_uses_bearer_header(self) -> None:
        dl = _make_downloader(token="ghp_token")
        dl.auth_resolver.resolve_for_dep.return_value.token = "ghp_token"
        dl.auth_resolver.resolve_for_dep.return_value.auth_scheme = "basic"
        dep = self._make_dep()
        dep.is_azure_devops.return_value = False
        dl.auth_resolver.classify_host.return_value = MagicMock(kind="github")

        attempts = _build_validation_attempts(dl, dep, lambda m: None)
        auth_attempts = [a for a in attempts if "header" in a.label.lower()]
        assert len(auth_attempts) >= 1
        env_values = list(auth_attempts[0].env.values())
        header_values = [v for v in env_values if isinstance(v, str) and ("Bearer" in v)]
        assert any("Bearer" in v for v in header_values)


# ---------------------------------------------------------------------------
# _ref_exists_via_ls_remote
# ---------------------------------------------------------------------------


class TestRefExistsViaLsRemote:
    def _log(self, _msg: str) -> None:
        pass

    def test_no_attempts_returns_false_none(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep()

        with patch.object(gdv, "_build_validation_attempts", return_value=[]):
            ok, attempt = _ref_exists_via_ls_remote(dl, dep, "main", self._log)

        assert ok is False
        assert attempt is None

    def test_successful_tag_match_returns_true_and_attempt(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep(ref="v1.0")

        mock_git = MagicMock()
        mock_git.ls_remote.return_value = "abc123\trefs/tags/v1.0\n"

        fake_attempt = AttemptSpec("test", "https://github.com/owner/repo.git", {})
        with (
            patch.object(gdv, "_build_validation_attempts", return_value=[fake_attempt]),
            patch("apm_cli.deps.github_downloader_validation.git") as mock_git_mod,
        ):
            mock_git_mod.cmd.Git.return_value = mock_git
            ok, _winning = _ref_exists_via_ls_remote(dl, dep, "v1.0", self._log)

        assert ok is True
        assert _winning == fake_attempt

    def test_sha_pin_scan_full_ref_list(self) -> None:
        dl = _make_downloader()
        dep = _make_github_dep(ref="abc1234")

        mock_git = MagicMock()
        mock_git.ls_remote.return_value = "abc1234def5678\trefs/heads/main\n"

        fake_attempt = AttemptSpec("test", "https://github.com/owner/repo.git", {})
        with (
            patch.object(gdv, "_build_validation_attempts", return_value=[fake_attempt]),
            patch("apm_cli.deps.github_downloader_validation.git") as mock_git_mod,
        ):
            mock_git_mod.cmd.Git.return_value = mock_git
            ok, _winning = _ref_exists_via_ls_remote(dl, dep, "abc1234", self._log)

        assert ok is True

    def test_all_attempts_fail_returns_false_none(self) -> None:
        import git as git_mod

        dl = _make_downloader()
        dep = _make_github_dep()

        mock_git = MagicMock()
        mock_git.ls_remote.side_effect = git_mod.exc.GitCommandError("ls-remote", 128, "err")

        fake_attempt = AttemptSpec("test", "https://github.com/owner/repo.git", {})
        with (
            patch.object(gdv, "_build_validation_attempts", return_value=[fake_attempt]),
            patch("apm_cli.deps.github_downloader_validation.git") as mock_git_mod,
        ):
            mock_git_mod.cmd.Git.return_value = mock_git
            mock_git_mod.exc.GitCommandError = git_mod.exc.GitCommandError
            ok, _winning = _ref_exists_via_ls_remote(dl, dep, "main", self._log)

        assert ok is False
        assert _winning is None


# ---------------------------------------------------------------------------
# _path_exists_in_tree_at_ref
# ---------------------------------------------------------------------------


class TestPathExistsInTreeAtRef:
    def _log(self, _msg: str) -> None:
        pass

    def _winning_attempt(self) -> AttemptSpec:
        return AttemptSpec("ssh", "git@github.com:owner/repo.git", {})

    def test_fetch_failure_returns_false(self, tmp_path: Path) -> None:
        import git as git_mod

        dl = _make_downloader()
        dep = _make_github_dep()
        attempt = self._winning_attempt()

        mock_git = MagicMock()
        mock_git.fetch.side_effect = git_mod.exc.GitCommandError("fetch", 128, "err")

        with (
            patch(
                "apm_cli.deps.github_downloader_validation.get_apm_temp_dir", return_value=tmp_path
            ),
            patch("apm_cli.deps.github_downloader_validation.git") as mock_git_mod,
        ):
            mock_git_mod.cmd.Git.return_value = mock_git
            mock_git_mod.exc.GitCommandError = git_mod.exc.GitCommandError
            result = _path_exists_in_tree_at_ref(dl, dep, "skills/foo", "main", self._log, attempt)

        assert result is False

    def test_ls_tree_empty_output_returns_false(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_github_dep()
        attempt = self._winning_attempt()

        mock_git = MagicMock()
        mock_git.fetch.return_value = ""
        mock_git.ls_tree.return_value = ""  # empty = path not found

        with (
            patch(
                "apm_cli.deps.github_downloader_validation.get_apm_temp_dir", return_value=tmp_path
            ),
            patch("apm_cli.deps.github_downloader_validation.git") as mock_git_mod,
        ):
            mock_git_mod.cmd.Git.return_value = mock_git
            result = _path_exists_in_tree_at_ref(dl, dep, "skills/foo", "main", self._log, attempt)

        assert result is False

    def test_ls_tree_with_output_returns_true(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_github_dep()
        attempt = self._winning_attempt()

        mock_git = MagicMock()
        mock_git.fetch.return_value = ""
        mock_git.ls_tree.return_value = "040000 tree abc123\tskills/foo\n"

        with (
            patch(
                "apm_cli.deps.github_downloader_validation.get_apm_temp_dir", return_value=tmp_path
            ),
            patch("apm_cli.deps.github_downloader_validation.git") as mock_git_mod,
        ):
            mock_git_mod.cmd.Git.return_value = mock_git
            result = _path_exists_in_tree_at_ref(dl, dep, "skills/foo", "main", self._log, attempt)

        assert result is True

    def test_ls_tree_exception_returns_false(self, tmp_path: Path) -> None:
        import git as git_mod

        dl = _make_downloader()
        dep = _make_github_dep()
        attempt = self._winning_attempt()

        mock_git = MagicMock()
        mock_git.fetch.return_value = ""
        mock_git.ls_tree.side_effect = git_mod.exc.GitCommandError("ls-tree", 128, "err")

        with (
            patch(
                "apm_cli.deps.github_downloader_validation.get_apm_temp_dir", return_value=tmp_path
            ),
            patch("apm_cli.deps.github_downloader_validation.git") as mock_git_mod,
        ):
            mock_git_mod.cmd.Git.return_value = mock_git
            mock_git_mod.exc.GitCommandError = git_mod.exc.GitCommandError
            result = _path_exists_in_tree_at_ref(dl, dep, "skills/foo", "main", self._log, attempt)

        assert result is False


# ---------------------------------------------------------------------------
# _ssh_attempt_allowed
# ---------------------------------------------------------------------------


class TestSshAttemptAllowed:
    def test_returns_false_by_default(self) -> None:
        dl = _make_downloader()
        # Default downloader has no SSH preference
        result = _ssh_attempt_allowed(dl)
        assert result is False

    def test_returns_true_when_ssh_preferred(self) -> None:
        from apm_cli.deps.transport_selection import ProtocolPreference

        dl = _make_downloader()
        dl._protocol_pref = ProtocolPreference.SSH
        dl._allow_fallback = False
        result = _ssh_attempt_allowed(dl)
        assert result is True

    def test_returns_true_when_fallback_allowed(self) -> None:
        dl = _make_downloader()
        dl._allow_fallback = True
        from apm_cli.deps.transport_selection import ProtocolPreference

        dl._protocol_pref = ProtocolPreference.HTTPS
        result = _ssh_attempt_allowed(dl)
        assert result is True


# ---------------------------------------------------------------------------
# _split_owner_repo edge cases
# ---------------------------------------------------------------------------


class TestSplitOwnerRepo:
    def test_valid_pair(self) -> None:
        assert _split_owner_repo("owner/repo") == ("owner", "repo")

    def test_no_slash_returns_none(self) -> None:
        assert _split_owner_repo("noslash") is None

    def test_empty_owner_returns_none(self) -> None:
        assert _split_owner_repo("/repo") is None

    def test_empty_repo_returns_none(self) -> None:
        assert _split_owner_repo("owner/") is None

    def test_multiple_slashes_split_on_first(self) -> None:
        # Only the first slash is the split point
        result = _split_owner_repo("owner/org/project/_git/repo")
        assert result is not None
        assert result[0] == "owner"
