"""Tree-at-ref path probes that clone into a scratch directory."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.github_downloader_validation import (
    AttemptSpec,
    _path_exists_in_tree_at_ref,
)
from apm_cli.models.apm_package import DependencyReference

pytestmark = pytest.mark.component

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


# ---------------------------------------------------------------------------
# validate_virtual_package_exists: warn_callback fired on git-fallback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _directory_exists_at_ref
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _build_validation_attempts
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _ref_exists_via_ls_remote
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _path_exists_in_tree_at_ref
# ---------------------------------------------------------------------------


class TestPathExistsInTreeAtRef:
    def _log(self, _msg: str) -> None:
        pass

    def _winning_attempt(self) -> AttemptSpec:
        return AttemptSpec("ssh", "git@github.com:owner/repo.git", {})

    def test_fetch_failure_returns_false(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_github_dep()
        attempt = self._winning_attempt()

        with (
            patch(
                "apm_cli.deps.github_downloader_validation.get_apm_temp_dir", return_value=tmp_path
            ),
            patch(
                "apm_cli.deps.github_downloader_validation.subprocess.run",
                side_effect=[
                    MagicMock(),
                    MagicMock(),
                    subprocess.CalledProcessError(128, ["git", "fetch"]),
                ],
            ),
        ):
            result = _path_exists_in_tree_at_ref(dl, dep, "skills/foo", "main", self._log, attempt)

        assert result is False

    def test_ls_tree_empty_output_returns_false(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_github_dep()
        attempt = self._winning_attempt()

        with (
            patch(
                "apm_cli.deps.github_downloader_validation.get_apm_temp_dir", return_value=tmp_path
            ),
            patch(
                "apm_cli.deps.github_downloader_validation.subprocess.run",
                return_value=MagicMock(stdout=""),
            ),
        ):
            result = _path_exists_in_tree_at_ref(dl, dep, "skills/foo", "main", self._log, attempt)

        assert result is False

    def test_ls_tree_with_output_returns_true(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_github_dep()
        attempt = self._winning_attempt()

        with (
            patch(
                "apm_cli.deps.github_downloader_validation.get_apm_temp_dir", return_value=tmp_path
            ),
            patch(
                "apm_cli.deps.github_downloader_validation.subprocess.run",
                return_value=MagicMock(stdout="040000 tree abc123\tskills/foo\n"),
            ),
        ):
            result = _path_exists_in_tree_at_ref(dl, dep, "skills/foo", "main", self._log, attempt)

        assert result is True

    def test_ls_tree_exception_returns_false(self, tmp_path: Path) -> None:
        dl = _make_downloader()
        dep = _make_github_dep()
        attempt = self._winning_attempt()

        with (
            patch(
                "apm_cli.deps.github_downloader_validation.get_apm_temp_dir", return_value=tmp_path
            ),
            patch(
                "apm_cli.deps.github_downloader_validation.subprocess.run",
                side_effect=[
                    MagicMock(),
                    MagicMock(),
                    MagicMock(),
                    subprocess.CalledProcessError(128, ["git", "ls-tree"]),
                ],
            ),
        ):
            result = _path_exists_in_tree_at_ref(dl, dep, "skills/foo", "main", self._log, attempt)

        assert result is False


# ---------------------------------------------------------------------------
# _ssh_attempt_allowed
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _split_owner_repo edge cases
# ---------------------------------------------------------------------------
