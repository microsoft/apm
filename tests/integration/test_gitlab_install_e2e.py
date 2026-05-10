"""Integration test: install a virtual file package from a GitLab source.

Closes the regression-trap gap flagged by the review panel for PR #1149:
no integration-with-fixtures tier coverage exercised the GitLab REST v4
raw-file path through ``GitHubPackageDownloader.download_package``. The
unit tests in ``tests/test_github_downloader.py`` cover the strategy
class directly, but they bypass ``download_package``, virtual-file
package materialisation, and lockfile entry construction.

This test wires those pieces together with stubs at the network seam
(``GitHubPackageDownloader._resilient_get``) so the install pipeline is
exercised end-to-end without hitting gitlab.com:

- ``GitHubPackageDownloader.download_package`` is invoked with a
  ``DependencyReference`` whose ``host == "gitlab.com"`` and whose
  ``virtual_path`` points at a single instructions file.
- The mock asserts the GitLab v4 raw-file API URL is used and captures
  the request headers.
- The lockfile entry is constructed from the resolved dependency via
  ``LockedDependency.from_dependency_ref`` and asserted to record
  ``host == "gitlab.com"`` (no leakage of the GitHub Contents API
  shape).
- Headers on the raw-file call must contain ``PRIVATE-TOKEN`` and must
  NOT contain ``Authorization`` -- the GitLab authentication contract
  is the regression trap that previously had no integration-tier
  coverage.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.lockfile import LockedDependency
from apm_cli.models.apm_package import DependencyReference

_CRED_FILL_PATCH = patch(
    "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
    return_value=None,
)

_GITLAB_FILE_BYTES = b"# Sample instructions\n\nHello from GitLab.\n"
_GITLAB_RESOLVED_SHA = "a" * 40


def _make_response(*, status: int = 200, content: bytes = b"", text: str = "") -> Mock:
    response = Mock()
    response.status_code = status
    response.content = content
    response.text = text
    response.raise_for_status = Mock()
    return response


def _resilient_get_stub(captured: list[dict]):
    """Build a ``_resilient_get`` replacement that captures and dispatches.

    Returns a callable that mimics the real signature
    ``(url, headers=..., timeout=...)`` used by both the GitLab raw-file
    download path and the commit SHA resolver.
    """

    def _stub(url, headers=None, timeout=None):
        captured.append({"url": url, "headers": dict(headers or {})})

        if "/repository/files/" in url and "/raw" in url:
            return _make_response(status=200, content=_GITLAB_FILE_BYTES)

        if "/repository/commits/" in url:
            return _make_response(status=200, text=_GITLAB_RESOLVED_SHA)

        return _make_response(status=404)

    return _stub


@pytest.mark.integration
class TestInstallFromGitLabIntegration:
    """End-to-end: GitHubPackageDownloader.download_package against a gitlab.com dep."""

    def setup_method(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.original_dir = Path.cwd()
        os.chdir(self.test_dir)

    def teardown_method(self):
        os.chdir(self.original_dir)
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_install_from_gitlab_com_virtual_path(self):
        """apm install for a gitlab.com virtual file package uses GitLab v4 + PRIVATE-TOKEN.

        Proves three contracts the unit tier alone could not:
        1. ``download_package`` routes a ``host="gitlab.com"`` dep through the
           GitLab REST v4 ``repository/files/.../raw`` endpoint (no GitHub
           Contents API URL leakage).
        2. The HTTP request that fetches the file content carries a
           ``PRIVATE-TOKEN`` header sourced from ``GITLAB_APM_PAT`` and does
           NOT carry an ``Authorization`` header (no cross-host leakage).
        3. A ``LockedDependency`` constructed from the resolved
           ``DependencyReference`` records ``host == "gitlab.com"``,
           preserving install-time host classification in the lockfile.
        """
        captured_calls: list[dict] = []

        env = {
            "GITLAB_APM_PAT": "glpat-integration-secret",
            "PATH": os.environ.get("PATH", ""),
        }
        with patch.dict(os.environ, env, clear=True), _CRED_FILL_PATCH:
            downloader = GitHubPackageDownloader()
            dep_ref = DependencyReference(
                repo_url="acme/standards",
                host="gitlab.com",
                virtual_path="skills/coding-style.instructions.md",
                is_virtual=True,
            )

            install_dir = self.test_dir / "apm_modules" / "acme" / "standards"

            with patch.object(
                downloader,
                "_resilient_get",
                side_effect=_resilient_get_stub(captured_calls),
            ):
                pkg_info = downloader.download_package(dep_ref, install_dir)

        # Materialised virtual file package on disk
        assert install_dir.exists()
        assert pkg_info is not None

        # At least one captured call hit the GitLab v4 raw-file endpoint
        raw_calls = [
            call
            for call in captured_calls
            if "gitlab.com/api/v4" in call["url"]
            and "/repository/files/" in call["url"]
            and "/raw" in call["url"]
        ]
        assert raw_calls, (
            f"Expected a GitLab v4 raw-file API call, captured: "
            f"{[call['url'] for call in captured_calls]}"
        )
        raw_call = raw_calls[0]

        # GitLab auth contract: PRIVATE-TOKEN header set, Authorization header absent
        assert "PRIVATE-TOKEN" in raw_call["headers"], (
            f"PRIVATE-TOKEN header missing from GitLab raw-file call: {raw_call['headers']}"
        )
        assert raw_call["headers"]["PRIVATE-TOKEN"] == "glpat-integration-secret"
        assert "Authorization" not in raw_call["headers"], (
            f"Authorization header must not leak onto GitLab raw-file call: {raw_call['headers']}"
        )

        # No GitHub Contents API URL leakage
        for call in captured_calls:
            assert "/repos/" not in call["url"] or "gitlab.com" not in call["url"]
            assert "contents/" not in call["url"]

        # Lockfile entry preserves host classification at install time
        locked = LockedDependency.from_dependency_ref(
            dep_ref=dep_ref,
            resolved_commit=_GITLAB_RESOLVED_SHA,
            depth=1,
            resolved_by=None,
        )
        assert locked.host == "gitlab.com"
        assert locked.repo_url == "acme/standards"
        assert locked.virtual_path == "skills/coding-style.instructions.md"
        assert locked.is_virtual is True
        assert locked.resolved_commit == _GITLAB_RESOLVED_SHA
