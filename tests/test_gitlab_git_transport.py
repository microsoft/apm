"""Tests for git-transport-first path: file fetching (issue #1014).

TDD acceptance tests encoding the new behavior:
- A git-sourced dep with a path: fetches that file via git sparse/partial
  checkout (no host REST API call).
- A self-hosted-GitLab-style API-410 source no longer fails.
- ensure_path_within() rejects a traversal/symlink-escaping path:.
- The thin GITLAB_PAT fallback path is exercised when git fails.

URL assertions use urllib.parse, never substring (per test contract).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from apm_cli.models.apm_package import DependencyReference
from apm_cli.utils.path_security import PathTraversalError

# Patch cred helper so tests never call real git for token resolution.
_CRED_FILL_PATCH = patch(
    "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
    return_value=None,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gitlab_dep(host: str = "gitlab.selfhosted.example.com") -> DependencyReference:
    return DependencyReference(repo_url="group/repo", host=host)


def _mock_subprocess_success() -> Mock:
    m = Mock()
    m.returncode = 0
    m.stderr = ""
    m.stdout = ""
    return m


# ---------------------------------------------------------------------------
# Unit tests for fetch_file_via_git_sparse
# ---------------------------------------------------------------------------


class TestFetchFileViaGitSparse:
    """Unit tests for the git_file_transport free function."""

    def test_path_traversal_in_string_rejected_before_git(self) -> None:
        """validate_path_segments() rejects path: containing .. sequences.

        No git subprocess is ever spawned; PathTraversalError raised first.
        """
        from apm_cli.deps.git_file_transport import fetch_file_via_git_sparse

        dep_ref = _make_gitlab_dep()
        with pytest.raises(PathTraversalError):
            fetch_file_via_git_sparse(
                dep_ref,
                "../../etc/passwd",
                "main",
                build_repo_url_fn=lambda *a, **kw: "https://gitlab.example.com/g/r.git",
                git_env={},
            )

    def test_path_traversal_encoded_rejected(self) -> None:
        """Double-encoded %2e%2e traversal is also rejected by validate_path_segments."""
        from apm_cli.deps.git_file_transport import fetch_file_via_git_sparse

        dep_ref = _make_gitlab_dep()
        with pytest.raises(PathTraversalError):
            fetch_file_via_git_sparse(
                dep_ref,
                "%2e%2e/etc/passwd",
                "main",
                build_repo_url_fn=lambda *a, **kw: "https://example.com/r.git",
                git_env={},
            )

    @patch("apm_cli.deps.git_file_transport.subprocess.run")
    def test_git_fetch_uses_blob_none_filter(self, mock_run: Mock, tmp_path: Path) -> None:
        """git fetch uses --filter=blob:none for partial object download."""
        from apm_cli.deps.git_file_transport import fetch_file_via_git_sparse

        work_parent = tmp_path / "fetch"
        work_parent.mkdir()
        work_dir = work_parent / "work"
        work_dir.mkdir()
        # Pre-create the file that simulated git checkout would produce.
        agents_dir = work_dir / "agents"
        agents_dir.mkdir()
        (agents_dir / "spec.agent.md").write_bytes(b"# Agent")

        mock_run.return_value = _mock_subprocess_success()

        with patch(
            "apm_cli.deps.git_file_transport.tempfile.mkdtemp",
            return_value=str(work_parent),
        ):
            result = fetch_file_via_git_sparse(
                _make_gitlab_dep(),
                "agents/spec.agent.md",
                "main",
                build_repo_url_fn=lambda *a, **kw: "https://gitlab.example.com/g/r.git",
                git_env={},
            )

        assert result == b"# Agent"
        all_cmds = [c[0][0] for c in mock_run.call_args_list]
        fetch_cmds = [c for c in all_cmds if "fetch" in c]
        assert fetch_cmds, "Expected at least one git fetch call"
        assert any("--filter=blob:none" in cmd for cmd in fetch_cmds)

    @patch("apm_cli.deps.git_file_transport.subprocess.run")
    def test_git_commands_not_rest_api(self, mock_run: Mock, tmp_path: Path) -> None:
        """File is fetched via git subprocess, not via requests.get / REST API."""
        from apm_cli.deps.git_file_transport import fetch_file_via_git_sparse

        work_parent = tmp_path / "fetch2"
        work_parent.mkdir()
        work_dir = work_parent / "work"
        work_dir.mkdir()
        agents_dir = work_dir / "agents"
        agents_dir.mkdir()
        (agents_dir / "api-specialist.agent.md").write_bytes(b"api agent content")

        mock_run.return_value = _mock_subprocess_success()

        with (
            patch(
                "apm_cli.deps.git_file_transport.tempfile.mkdtemp",
                return_value=str(work_parent),
            ),
            patch("requests.get") as mock_requests_get,
        ):
            result = fetch_file_via_git_sparse(
                _make_gitlab_dep(),
                "agents/api-specialist.agent.md",
                "main",
                build_repo_url_fn=lambda *a, **kw: "https://gitlab.example.com/g/r.git",
                git_env={},
            )

        assert result == b"api agent content"
        mock_requests_get.assert_not_called()

    @patch("apm_cli.deps.git_file_transport.subprocess.run")
    def test_symlink_escape_rejected_by_ensure_path_within(
        self, mock_run: Mock, tmp_path: Path
    ) -> None:
        """ensure_path_within() rejects a symlink in the checkout that escapes the work tree."""
        from apm_cli.deps.git_file_transport import fetch_file_via_git_sparse

        work_parent = tmp_path / "fetch3"
        work_parent.mkdir()
        work_dir = work_parent / "work"
        work_dir.mkdir()

        # Create a file outside the work dir.
        outside = tmp_path / "outside.txt"
        outside.write_text("should not be read")

        # Create a symlink inside the work dir pointing outside.
        (work_dir / "agents").mkdir()
        (work_dir / "agents" / "evil.agent.md").symlink_to(outside)

        mock_run.return_value = _mock_subprocess_success()

        with (
            patch(
                "apm_cli.deps.git_file_transport.tempfile.mkdtemp",
                return_value=str(work_parent),
            ),
            pytest.raises(PathTraversalError),
        ):
            fetch_file_via_git_sparse(
                _make_gitlab_dep(),
                "agents/evil.agent.md",
                "main",
                build_repo_url_fn=lambda *a, **kw: "https://gitlab.example.com/g/r.git",
                git_env={},
            )

    @patch("apm_cli.deps.git_file_transport.subprocess.run")
    def test_root_level_file_fetched_without_sparse_cone(
        self, mock_run: Mock, tmp_path: Path
    ) -> None:
        """Root-level files (no parent dir) skip cone sparse-checkout setup."""
        from apm_cli.deps.git_file_transport import fetch_file_via_git_sparse

        work_parent = tmp_path / "fetch4"
        work_parent.mkdir()
        work_dir = work_parent / "work"
        work_dir.mkdir()
        (work_dir / "root.agent.md").write_bytes(b"root agent")

        mock_run.return_value = _mock_subprocess_success()

        with patch(
            "apm_cli.deps.git_file_transport.tempfile.mkdtemp",
            return_value=str(work_parent),
        ):
            result = fetch_file_via_git_sparse(
                _make_gitlab_dep(),
                "root.agent.md",
                "main",
                build_repo_url_fn=lambda *a, **kw: "https://gitlab.example.com/g/r.git",
                git_env={},
            )

        assert result == b"root agent"
        all_cmds = [c[0][0] for c in mock_run.call_args_list]
        # No sparse-checkout init call for root files.
        sparse_inits = [c for c in all_cmds if "sparse-checkout" in c and "init" in c]
        assert not sparse_inits, (
            f"Expected no sparse-checkout init for root file, got: {sparse_inits}"
        )

    @patch("apm_cli.deps.git_file_transport.subprocess.run")
    def test_git_failure_raises_runtime_error(self, mock_run: Mock, tmp_path: Path) -> None:
        """RuntimeError is raised when a git command fails."""
        from apm_cli.deps.git_file_transport import fetch_file_via_git_sparse

        mock_run.return_value = Mock(
            returncode=128, stderr="fatal: not a git repository", stdout=""
        )

        work_parent = tmp_path / "fetch5"
        work_parent.mkdir()

        with (
            patch(
                "apm_cli.deps.git_file_transport.tempfile.mkdtemp",
                return_value=str(work_parent),
            ),
            pytest.raises(RuntimeError, match="git file fetch failed"),
        ):
            fetch_file_via_git_sparse(
                _make_gitlab_dep(),
                "agents/spec.agent.md",
                "main",
                build_repo_url_fn=lambda *a, **kw: "https://gitlab.example.com/g/r.git",
                git_env={},
            )

    @patch("apm_cli.deps.git_file_transport.subprocess.run")
    def test_file_missing_after_checkout_raises_runtime_error(
        self, mock_run: Mock, tmp_path: Path
    ) -> None:
        """RuntimeError raised when file is absent after successful git checkout."""
        from apm_cli.deps.git_file_transport import fetch_file_via_git_sparse

        work_parent = tmp_path / "fetch6"
        work_parent.mkdir()
        work_dir = work_parent / "work"
        work_dir.mkdir()
        # Do NOT create the expected file (simulates git not finding it in sparse cone).

        mock_run.return_value = _mock_subprocess_success()

        with (
            patch(
                "apm_cli.deps.git_file_transport.tempfile.mkdtemp",
                return_value=str(work_parent),
            ),
            pytest.raises(RuntimeError, match="not found after git sparse checkout"),
        ):
            fetch_file_via_git_sparse(
                _make_gitlab_dep(),
                "agents/missing.agent.md",
                "main",
                build_repo_url_fn=lambda *a, **kw: "https://gitlab.example.com/g/r.git",
                git_env={},
            )


# ---------------------------------------------------------------------------
# Integration tests via download_strategies.DownloadDelegate
# ---------------------------------------------------------------------------


class TestGitlabGitTransportIntegration:
    """Integration tests: download_gitlab_file tries git first, REST API second."""

    def _make_downloader(self, env: dict):
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        return GitHubPackageDownloader()

    def test_self_hosted_gitlab_410_no_longer_fails(self) -> None:
        """Primary acceptance: self-hosted GitLab API-410 succeeds via git transport."""
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        # Self-hosted GitLab: GITLAB_HOST env var is required for APM to
        # classify the hostname as "gitlab" (same requirement as installs).
        self_hosted = "gitlab.selfhosted.example.com"
        dep_ref = _make_gitlab_dep(self_hosted)
        expected = b"# Self-hosted GitLab agent"

        with (
            patch.dict(
                os.environ,
                {"GITLAB_HOST": self_hosted},
                clear=True,
            ),
            _CRED_FILL_PATCH,
        ):
            downloader = GitHubPackageDownloader()
            with (
                patch(
                    "apm_cli.deps.download_strategies.fetch_file_via_git_sparse",
                    return_value=expected,
                ) as mock_git,
                patch.object(downloader, "_resilient_get") as mock_api,
            ):
                result = downloader._download_github_file(
                    dep_ref, "agents/api-specialist.agent.md", "main"
                )

        assert result == expected
        mock_git.assert_called_once()
        mock_api.assert_not_called()

    def test_gitlab_path_fetched_via_git_not_api(self) -> None:
        """git.com/GitLab-classified host: path: file fetched via git, no REST API."""
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        dep_ref = DependencyReference(repo_url="group/sub/repo", host="gitlab.com")
        expected = b"gitlab.com agent content"

        with (
            patch.dict(os.environ, {}, clear=True),
            _CRED_FILL_PATCH,
        ):
            downloader = GitHubPackageDownloader()
            with (
                patch(
                    "apm_cli.deps.download_strategies.fetch_file_via_git_sparse",
                    return_value=expected,
                ) as mock_git,
                patch.object(downloader, "_resilient_get") as mock_api,
            ):
                result = downloader._download_github_file(dep_ref, "agents/spec.agent.md", "main")

        assert result == expected
        mock_git.assert_called_once()
        mock_api.assert_not_called()

    def test_gitlab_pat_fallback_when_git_fails(self) -> None:
        """Thin GITLAB_PAT fallback: REST API called when git transport raises."""
        from urllib.parse import urlparse

        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        dep_ref = _make_gitlab_dep("gitlab.com")
        expected_bytes = b"fallback via REST API"

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = expected_bytes
        mock_response.raise_for_status = Mock()

        with (
            patch.dict(os.environ, {"GITLAB_APM_PAT": "glpat-fallback-token"}, clear=True),
            _CRED_FILL_PATCH,
        ):
            downloader = GitHubPackageDownloader()
            with (
                patch(
                    "apm_cli.deps.download_strategies.fetch_file_via_git_sparse",
                    side_effect=RuntimeError("git transport failed"),
                ),
                patch.object(downloader, "_resilient_get", return_value=mock_response) as mock_api,
            ):
                result = downloader._download_github_file(
                    dep_ref, "agents/fallback.agent.md", "main"
                )

        assert result == expected_bytes
        mock_api.assert_called_once()
        api_url = mock_api.call_args[0][0]
        parsed = urlparse(api_url)
        # Must be the GitLab v4 API endpoint, not GitHub Contents API.
        assert parsed.scheme == "https"
        assert "api/v4" in parsed.path
        assert "repository/files" in parsed.path

    def test_gitlab_pat_absent_and_git_fails_raises(self) -> None:
        """When git fails and no GITLAB_PAT is set, a descriptive RuntimeError is raised."""
        from apm_cli.deps.github_downloader import GitHubPackageDownloader

        dep_ref = _make_gitlab_dep()

        with (
            patch.dict(os.environ, {}, clear=True),
            _CRED_FILL_PATCH,
        ):
            downloader = GitHubPackageDownloader()
            with (
                patch(
                    "apm_cli.deps.download_strategies.fetch_file_via_git_sparse",
                    side_effect=RuntimeError("SSH auth failed"),
                ),
                patch.object(downloader, "_resilient_get") as mock_api,
            ):
                # REST API has no token, so it should either fail with auth error
                # or not be called. Either way, a RuntimeError must propagate.
                mock_api.side_effect = RuntimeError("401 Unauthorized")
                with pytest.raises(RuntimeError):
                    downloader._download_github_file(dep_ref, "agents/spec.agent.md", "main")
