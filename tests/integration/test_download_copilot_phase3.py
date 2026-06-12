"""Integration tests for download_strategies.py and copilot.py — phase 3 coverage push.

Exercises real code paths with MINIMAL mocking.
Only external I/O (HTTP, subprocess, filesystem, os.environ, Path.home) is mocked.

Covers:
  - DownloadDelegate.resilient_get  (rate-limit handling, retry logic, error paths)
  - DownloadDelegate.build_repo_url  (GitHub, ADO, GitLab, insecure, SSH)
  - DownloadDelegate.get_artifactory_headers
  - DownloadDelegate.download_artifactory_archive  (success, zip extraction, path traversal guard)
  - DownloadDelegate.download_file_from_artifactory  (entry API, archive fallback)
  - DownloadDelegate.try_raw_download
  - DownloadDelegate.download_ado_file  (success, 404 fallback, 401/403, network error)
  - DownloadDelegate.download_gitlab_file  (success, 404 fallback, auth error)
  - DownloadDelegate.download_github_file  (CDN fast-path, Contents API, fallback branches)
  - DownloadDelegate._is_configured_ghes
  - DownloadDelegate._build_contents_api_urls  (github.com, ghe.com, ghes, generic)
  - DownloadDelegate._build_generic_host_auth_headers
  - DownloadDelegate._extract_contents_api_payload  (github, generic JSON, base64)
  - DownloadDelegate._build_unsupported_or_missing_error
  - CopilotClientAdapter helpers: _translate_env_placeholder, _extract_legacy_angle_vars,
    _has_env_placeholder, _stringify_env_literal
  - CopilotClientAdapter.get_config_path / get_current_config / update_config
  - CopilotClientAdapter.configure_mcp_server  (npm, docker, remote, raw-stdio)
  - CopilotClientAdapter._format_server_config  (remotes, packages, raw-stdio)
  - CopilotClientAdapter._resolve_environment_variables  (translate mode, list & dict)
  - CopilotClientAdapter._resolve_variable_placeholders  (translate mode)
  - CopilotClientAdapter._select_best_package / _select_remote_with_url
  - CopilotClientAdapter._dispatch_package_to_config  (npm, docker, pypi)
  - CopilotClientAdapter._inject_env_vars_into_docker_args
  - CopilotClientAdapter._process_arguments  (positional, named, string)
  - CopilotClientAdapter._is_github_server
  - CopilotClientAdapter.emit_install_run_summary / reset_install_run_state
  - CopilotClientAdapter._collect_previously_baked_keys
  - CopilotClientAdapter._emit_install_summary
"""

from __future__ import annotations

import base64
import io
import json
import os
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.adapters.client.copilot import (
    CopilotClientAdapter,
    _extract_legacy_angle_vars,
    _has_env_placeholder,
    _stringify_env_literal,
    _translate_env_placeholder,
)
from apm_cli.deps.download_strategies import DownloadDelegate
from apm_cli.models.dependency.reference import DependencyReference

# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------


def _make_host(
    *,
    github_host: str = "github.com",
    github_token: str | None = None,
    ado_token: str | None = None,
    artifactory_token: str | None = None,
    registry_config=None,
) -> MagicMock:
    """Build a minimal mock GitHubPackageDownloader host for DownloadDelegate."""
    host = MagicMock()
    host.github_host = github_host
    host.github_token = github_token
    host.ado_token = ado_token
    host.artifactory_token = artifactory_token
    host.registry_config = registry_config
    host.auth_resolver = MagicMock()
    host.auth_resolver.resolve_for_dep.return_value = MagicMock(token=None)
    host.auth_resolver.resolve.return_value = MagicMock(token=None, source=None)
    host.auth_resolver.classify_host.return_value = MagicMock(
        kind="github",
        has_public_repos=True,
        api_base="https://api.github.com",
    )
    host.auth_resolver.build_error_context.return_value = "Use GITHUB_APM_PAT."
    host._resolve_dep_auth_ctx = MagicMock(
        side_effect=lambda *a, **k: host.auth_resolver.resolve.return_value
    )
    return host


def _make_dep_ref(
    repo_url: str = "owner/repo",
    host: str | None = "github.com",
    port: int | None = None,
    ado_org: str | None = None,
    ado_project: str | None = None,
    ado_repo: str | None = None,
    is_insecure: bool = False,
    explicit_scheme: str | None = None,
) -> DependencyReference:
    """Create a minimal DependencyReference for tests."""
    return DependencyReference(
        repo_url=repo_url,
        host=host,
        port=port,
        ado_organization=ado_org,
        ado_project=ado_project,
        ado_repo=ado_repo,
        is_insecure=is_insecure,
        explicit_scheme=explicit_scheme,
    )


def _make_mock_response(
    status_code: int = 200,
    content: bytes = b"data",
    headers: dict | None = None,
) -> MagicMock:
    """Build a minimal mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.content = content
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        http_err = requests.exceptions.HTTPError(response=resp)
        resp.raise_for_status.side_effect = http_err
    return resp


def _make_zip_bytes(files: dict[str, bytes], root_prefix: str = "repo-main/") -> bytes:
    """Create a zip archive with a root prefix directory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # Add root directory entry
        info = zipfile.ZipInfo(root_prefix)
        zf.writestr(info, b"")
        for name, data in files.items():
            zf.writestr(root_prefix + name, data)
    return buf.getvalue()


# ===========================================================================
# DownloadDelegate — module-level debug helper
# ===========================================================================


class TestDebugHelper:
    """_debug helper emits to stderr only when APM_DEBUG is set."""

    def test_debug_no_output_when_env_unset(self, capsys: pytest.CaptureFixture) -> None:
        """_debug produces no output when APM_DEBUG is absent."""
        from apm_cli.deps.download_strategies import _debug

        with patch.dict(os.environ, {}, clear=True):
            # Ensure APM_DEBUG is absent
            os.environ.pop("APM_DEBUG", None)
            _debug("silent message")
        captured = capsys.readouterr()
        assert "silent message" not in captured.err

    def test_debug_writes_to_stderr_when_env_set(self, capsys: pytest.CaptureFixture) -> None:
        """_debug writes to stderr when APM_DEBUG is set."""
        from apm_cli.deps.download_strategies import _debug

        with patch.dict(os.environ, {"APM_DEBUG": "1"}):
            _debug("test message")
        captured = capsys.readouterr()
        assert "test message" in captured.err


# ===========================================================================
# DownloadDelegate — resilient_get
# ===========================================================================


class TestResilientGet:
    """HTTP resilient GET with retry, rate-limit, and error handling."""

    def test_success_on_first_attempt(self) -> None:
        """Returns response immediately on HTTP 200."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        resp = _make_mock_response(200)
        with patch("apm_cli.deps.download_strategies.requests.get", return_value=resp) as mock_get:
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=3)
        assert result.status_code == 200
        mock_get.assert_called_once()

    def test_retries_on_429_with_retry_after_header(self) -> None:
        """Retries after Retry-After seconds when 429 received."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        rate_limited = _make_mock_response(429, headers={"Retry-After": "0.01"})
        success = _make_mock_response(200)
        with (
            patch("apm_cli.deps.download_strategies.requests.get") as mock_get,
            patch("apm_cli.deps.download_strategies.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = [rate_limited, success]
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=3)
        assert result.status_code == 200
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once()

    def test_retries_on_503(self) -> None:
        """Retries on 503 Service Unavailable."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        unavailable = _make_mock_response(503, headers={})
        success = _make_mock_response(200)
        with (
            patch("apm_cli.deps.download_strategies.requests.get") as mock_get,
            patch("apm_cli.deps.download_strategies.time.sleep"),
        ):
            mock_get.side_effect = [unavailable, success]
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=3)
        assert result.status_code == 200

    def test_rate_limited_via_403_with_ratelimit_remaining_zero(self) -> None:
        """Treats 403 with X-RateLimit-Remaining: 0 as rate limit."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        forbidden = _make_mock_response(
            403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999999"}
        )
        success = _make_mock_response(200)
        with (
            patch("apm_cli.deps.download_strategies.requests.get") as mock_get,
            patch("apm_cli.deps.download_strategies.time.sleep"),
        ):
            mock_get.side_effect = [forbidden, success]
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=3)
        assert result.status_code == 200

    def test_retries_on_connection_error(self) -> None:
        """Retries once on ConnectionError then succeeds."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        success = _make_mock_response(200)
        with (
            patch("apm_cli.deps.download_strategies.requests.get") as mock_get,
            patch("apm_cli.deps.download_strategies.time.sleep"),
        ):
            mock_get.side_effect = [requests.exceptions.ConnectionError("down"), success]
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=3)
        assert result.status_code == 200

    def test_retries_on_timeout(self) -> None:
        """Retries on Timeout then succeeds."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        success = _make_mock_response(200)
        with (
            patch("apm_cli.deps.download_strategies.requests.get") as mock_get,
            patch("apm_cli.deps.download_strategies.time.sleep"),
        ):
            mock_get.side_effect = [requests.exceptions.Timeout("timed out"), success]
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=3)
        assert result.status_code == 200

    def test_exhausts_retries_raises_connection_error(self) -> None:
        """Raises ConnectionError after all retries exhausted."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        with (
            patch("apm_cli.deps.download_strategies.requests.get") as mock_get,
            patch("apm_cli.deps.download_strategies.time.sleep"),
        ):
            mock_get.side_effect = requests.exceptions.ConnectionError("always fails")
            with pytest.raises(requests.exceptions.ConnectionError):
                delegate.resilient_get("https://api.github.com/test", {}, max_retries=2)

    def test_all_rate_limited_returns_last_response(self) -> None:
        """Returns last rate-limited response when all retries are exhausted by rate limits."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        rate_limited = _make_mock_response(429, headers={"Retry-After": "0.01"})
        with (
            patch("apm_cli.deps.download_strategies.requests.get", return_value=rate_limited),
            patch("apm_cli.deps.download_strategies.time.sleep"),
        ):
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=2)
        assert result.status_code == 429

    def test_retry_after_invalid_falls_back_to_exponential(self) -> None:
        """Non-numeric Retry-After falls back to exponential backoff."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        rate_limited = _make_mock_response(
            429, headers={"Retry-After": "Thu, 01 Jan 2099 00:00:00 GMT"}
        )
        success = _make_mock_response(200)
        with (
            patch("apm_cli.deps.download_strategies.requests.get") as mock_get,
            patch("apm_cli.deps.download_strategies.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = [rate_limited, success]
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=3)
        assert result.status_code == 200
        mock_sleep.assert_called_once()

    def test_ratelimit_remaining_low_logged(self, capsys: pytest.CaptureFixture) -> None:
        """Low X-RateLimit-Remaining triggers debug log when APM_DEBUG set."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        resp = _make_mock_response(200, headers={"X-RateLimit-Remaining": "5"})
        with (
            patch("apm_cli.deps.download_strategies.requests.get", return_value=resp),
            patch.dict(os.environ, {"APM_DEBUG": "1"}),
        ):
            delegate.resilient_get("https://api.github.com/test", {}, max_retries=1)
        captured = capsys.readouterr()
        assert "rate limit low" in captured.err

    def test_x_ratelimit_reset_header_used_for_wait(self) -> None:
        """X-RateLimit-Reset used to compute wait when Retry-After absent."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        import time as _time

        future_reset = str(int(_time.time()) + 5)
        rate_limited = _make_mock_response(429, headers={"X-RateLimit-Reset": future_reset})
        success = _make_mock_response(200)
        with (
            patch("apm_cli.deps.download_strategies.requests.get") as mock_get,
            patch("apm_cli.deps.download_strategies.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = [rate_limited, success]
            result = delegate.resilient_get("https://api.github.com/test", {}, max_retries=3)
        assert result.status_code == 200
        mock_sleep.assert_called_once()


# ===========================================================================
# DownloadDelegate — build_repo_url
# ===========================================================================


class TestBuildRepoUrl:
    """URL construction for different backends and configurations."""

    def test_github_https_with_token(self) -> None:
        """Builds authenticated GitHub HTTPS clone URL."""
        from urllib.parse import urlparse

        host = _make_host(github_host="github.com", github_token="mytoken")
        backend_mock = MagicMock()
        backend_mock.kind = "github"
        backend_mock.is_github_family = True
        backend_mock.build_clone_https_url.return_value = (
            "https://mytoken@github.com/owner/repo.git"
        )
        with patch("apm_cli.deps.download_strategies.backend_for", return_value=backend_mock):
            delegate = DownloadDelegate(host)
            dep = _make_dep_ref("owner/repo", host="github.com")
            url = delegate.build_repo_url("owner/repo", dep_ref=dep)
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert "github.com" in parsed.netloc

    def test_github_ssh_url(self) -> None:
        """Builds SSH URL when use_ssh=True."""
        host = _make_host(github_host="github.com", github_token="tok")
        backend_mock = MagicMock()
        backend_mock.kind = "github"
        backend_mock.is_github_family = True
        backend_mock.build_clone_ssh_url.return_value = "git@github.com:owner/repo.git"
        with patch("apm_cli.deps.download_strategies.backend_for", return_value=backend_mock):
            delegate = DownloadDelegate(host)
            dep = _make_dep_ref("owner/repo", host="github.com")
            url = delegate.build_repo_url("owner/repo", use_ssh=True, dep_ref=dep)
        assert url.startswith("git@")

    def test_insecure_url_uses_http(self) -> None:
        """Insecure dep_ref builds http:// URL."""
        host = _make_host()
        backend_mock = MagicMock()
        backend_mock.kind = "github"
        backend_mock.is_github_family = True
        backend_mock.build_clone_http_url.return_value = "http://myhost.example.com/owner/repo.git"
        with patch("apm_cli.deps.download_strategies.backend_for", return_value=backend_mock):
            delegate = DownloadDelegate(host)
            dep = _make_dep_ref("owner/repo", host="myhost.example.com", is_insecure=True)
            url = delegate.build_repo_url("owner/repo", dep_ref=dep)
        from urllib.parse import urlparse

        parsed = urlparse(url)
        assert parsed.scheme == "http"

    def test_no_dep_ref_uses_ssh_url(self) -> None:
        """Legacy no-dep_ref path: SSH URL when use_ssh=True."""

        host = _make_host(github_host="github.com", github_token=None)
        backend_mock = MagicMock()
        backend_mock.kind = "github"
        backend_mock.is_github_family = True
        with patch("apm_cli.deps.download_strategies.backend_for", return_value=backend_mock):
            delegate = DownloadDelegate(host)
            url = delegate.build_repo_url("owner/repo", use_ssh=True, dep_ref=None)
        assert "owner/repo" in url or "git@" in url

    def test_no_dep_ref_https_without_token(self) -> None:
        """Legacy no-dep_ref: HTTPS without embedding a token when no token."""
        from urllib.parse import urlparse

        host = _make_host(github_host="github.com", github_token=None)
        backend_mock = MagicMock()
        backend_mock.kind = "github"
        backend_mock.is_github_family = True
        with patch("apm_cli.deps.download_strategies.backend_for", return_value=backend_mock):
            delegate = DownloadDelegate(host)
            url = delegate.build_repo_url("owner/repo", dep_ref=None)
        assert url  # Should produce some URL
        parsed = urlparse(url)
        assert parsed.scheme in ("https", "http")

    def test_empty_token_suppresses_credential(self) -> None:
        """token='' means 'suppress token' — HTTPS URL has no credential."""
        host = _make_host(github_token="should_not_appear")
        backend_mock = MagicMock()
        backend_mock.kind = "github"
        backend_mock.is_github_family = True
        backend_mock.build_clone_https_url.return_value = "https://github.com/owner/repo.git"
        with patch("apm_cli.deps.download_strategies.backend_for", return_value=backend_mock):
            delegate = DownloadDelegate(host)
            dep = _make_dep_ref("owner/repo", host="github.com")
            url = delegate.build_repo_url("owner/repo", dep_ref=dep, token="")
        assert "should_not_appear" not in url


# ===========================================================================
# DownloadDelegate — get_artifactory_headers
# ===========================================================================


class TestGetArtifactoryHeaders:
    """Artifactory header construction delegates to registry_config or fallback."""

    def test_uses_registry_config_headers(self) -> None:
        """Delegates to registry_config.get_headers() when available."""
        cfg = MagicMock()
        cfg.get_headers.return_value = {"Authorization": "Bearer reg-token"}
        host = _make_host(registry_config=cfg)
        delegate = DownloadDelegate(host)
        hdrs = delegate.get_artifactory_headers()
        assert hdrs == {"Authorization": "Bearer reg-token"}
        cfg.get_headers.assert_called_once()

    def test_falls_back_to_artifactory_token(self) -> None:
        """Falls back to direct artifactory_token when no registry_config."""
        host = _make_host(artifactory_token="direct-token", registry_config=None)
        delegate = DownloadDelegate(host)
        hdrs = delegate.get_artifactory_headers()
        assert hdrs == {"Authorization": "Bearer direct-token"}

    def test_empty_headers_when_no_token_or_config(self) -> None:
        """Returns empty dict when no token and no registry_config."""
        host = _make_host(artifactory_token=None, registry_config=None)
        delegate = DownloadDelegate(host)
        hdrs = delegate.get_artifactory_headers()
        assert hdrs == {}


# ===========================================================================
# DownloadDelegate — download_artifactory_archive
# ===========================================================================


class TestDownloadArtifactoryArchive:
    """ZIP archive download, extraction, and path-traversal guard."""

    def test_successful_extraction_strips_root_prefix(self, tmp_path: Path) -> None:
        """Extracts archive files, stripping the root directory prefix."""
        zip_data = _make_zip_bytes({"apm.yml": b"packages: []", "README.md": b"# Test"})
        host = _make_host(artifactory_token="tok")
        delegate = DownloadDelegate(host)
        mock_resp = _make_mock_response(200, content=zip_data)
        host._resilient_get = MagicMock(return_value=mock_resp)

        with patch("apm_cli.deps.download_strategies.build_artifactory_archive_url") as mock_url:
            mock_url.return_value = ["https://art.example.com/owner/repo/archive/main.zip"]
            delegate.download_artifactory_archive(
                host="art.example.com",
                prefix="artifactory/github",
                owner="owner",
                repo="repo",
                ref="main",
                target_path=tmp_path / "out",
            )

        assert (tmp_path / "out" / "apm.yml").read_bytes() == b"packages: []"
        assert (tmp_path / "out" / "README.md").read_bytes() == b"# Test"

    def test_raises_on_http_error(self, tmp_path: Path) -> None:
        """Raises RuntimeError when all URL attempts return non-200."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        mock_resp = _make_mock_response(404)
        host._resilient_get = MagicMock(return_value=mock_resp)

        with patch("apm_cli.deps.download_strategies.build_artifactory_archive_url") as mock_url:
            mock_url.return_value = ["https://art.example.com/owner/repo/archive/main.zip"]
            with pytest.raises(RuntimeError, match="Failed to download package"):
                delegate.download_artifactory_archive(
                    host="art.example.com",
                    prefix="artifactory/github",
                    owner="owner",
                    repo="repo",
                    ref="main",
                    target_path=tmp_path / "out",
                )

    def test_raises_on_invalid_zip(self, tmp_path: Path) -> None:
        """Raises RuntimeError on bad zip file."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        mock_resp = _make_mock_response(200, content=b"not a zip file")
        host._resilient_get = MagicMock(return_value=mock_resp)

        with patch("apm_cli.deps.download_strategies.build_artifactory_archive_url") as mock_url:
            mock_url.return_value = ["https://art.example.com/owner/repo/archive/main.zip"]
            with pytest.raises(RuntimeError):
                delegate.download_artifactory_archive(
                    host="art.example.com",
                    prefix="artifactory/github",
                    owner="owner",
                    repo="repo",
                    ref="main",
                    target_path=tmp_path / "out",
                )

    def test_archive_too_large_skipped(self, tmp_path: Path) -> None:
        """Archives exceeding size limit raise RuntimeError."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        # Create a small but "large" zip (override the env var for 1 byte limit)
        zip_data = _make_zip_bytes({"file.txt": b"x"})
        mock_resp = _make_mock_response(200, content=zip_data)
        host._resilient_get = MagicMock(return_value=mock_resp)

        with (
            patch("apm_cli.deps.download_strategies.build_artifactory_archive_url") as mock_url,
            patch.dict(os.environ, {"ARTIFACTORY_MAX_ARCHIVE_MB": "0"}),
        ):
            mock_url.return_value = ["https://art.example.com/owner/repo/archive/main.zip"]
            with pytest.raises(RuntimeError):
                delegate.download_artifactory_archive(
                    host="art.example.com",
                    prefix="artifactory/github",
                    owner="owner",
                    repo="repo",
                    ref="main",
                    target_path=tmp_path / "out",
                )

    def test_single_file_archive_extracted_asis(self, tmp_path: Path) -> None:
        """Single-file archive (no root directory) is extracted as-is."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("standalone.txt", b"content")
        zip_data = buf.getvalue()
        host = _make_host()
        delegate = DownloadDelegate(host)
        mock_resp = _make_mock_response(200, content=zip_data)
        host._resilient_get = MagicMock(return_value=mock_resp)

        with patch("apm_cli.deps.download_strategies.build_artifactory_archive_url") as mock_url:
            mock_url.return_value = ["https://art.example.com/owner/repo/archive/main.zip"]
            delegate.download_artifactory_archive(
                host="art.example.com",
                prefix="artifactory/github",
                owner="owner",
                repo="repo",
                ref="main",
                target_path=tmp_path / "out",
            )
        assert (tmp_path / "out" / "standalone.txt").read_bytes() == b"content"

    def test_tries_second_url_on_first_failure(self, tmp_path: Path) -> None:
        """Falls through to second URL when first returns 404."""
        zip_data = _make_zip_bytes({"apm.yml": b"packages: []"})
        host = _make_host()
        delegate = DownloadDelegate(host)
        fail_resp = _make_mock_response(404)
        ok_resp = _make_mock_response(200, content=zip_data)
        host._resilient_get = MagicMock(side_effect=[fail_resp, ok_resp])

        with patch("apm_cli.deps.download_strategies.build_artifactory_archive_url") as mock_url:
            mock_url.return_value = [
                "https://art.example.com/url1",
                "https://art.example.com/url2",
            ]
            delegate.download_artifactory_archive(
                host="art.example.com",
                prefix="artifactory/github",
                owner="owner",
                repo="repo",
                ref="main",
                target_path=tmp_path / "out",
            )
        assert (tmp_path / "out" / "apm.yml").read_bytes() == b"packages: []"


# ===========================================================================
# DownloadDelegate — download_file_from_artifactory
# ===========================================================================


class TestDownloadFileFromArtifactory:
    """Single-file download from Artifactory — entry API and archive fallback."""

    def test_uses_registry_config_client(self) -> None:
        """Delegates to registry_config client.fetch_file when available."""
        cfg = MagicMock()
        cfg.host = "art.example.com"
        cfg.get_client.return_value.fetch_file.return_value = b"file content"
        host = _make_host(registry_config=cfg)
        delegate = DownloadDelegate(host)

        result = delegate.download_file_from_artifactory(
            host="art.example.com",
            prefix="artifactory/github",
            owner="owner",
            repo="repo",
            file_path="apm.yml",
            ref="main",
        )
        assert result == b"file content"

    def test_fallback_to_archive_download(self) -> None:
        """Falls back to archive download when entry API returns None (no registry_config)."""
        host = _make_host(registry_config=None)
        delegate = DownloadDelegate(host)
        # Build a zip with root_prefix/apm.yml
        zip_data = _make_zip_bytes({"apm.yml": b"packages: []"})
        ok_resp = _make_mock_response(200, content=zip_data)
        host._resilient_get = MagicMock(return_value=ok_resp)

        # Patch the deferred-import helper to return None so the archive path is taken
        with (
            patch("apm_cli.deps.artifactory_entry.fetch_entry_from_archive", return_value=None),
            patch("apm_cli.deps.download_strategies.build_artifactory_archive_url") as mock_url,
        ):
            mock_url.return_value = ["https://art.example.com/url1"]
            result = delegate.download_file_from_artifactory(
                host="art.example.com",
                prefix="artifactory/github",
                owner="owner",
                repo="repo",
                file_path="apm.yml",
                ref="main",
            )
        assert result == b"packages: []"

    def test_raises_when_file_not_in_archive(self) -> None:
        """Raises RuntimeError when file is absent from the archive."""
        host = _make_host(registry_config=None)
        delegate = DownloadDelegate(host)
        zip_data = _make_zip_bytes({"other_file.txt": b"data"})
        ok_resp = _make_mock_response(200, content=zip_data)
        host._resilient_get = MagicMock(return_value=ok_resp)

        with (
            patch("apm_cli.deps.artifactory_entry.fetch_entry_from_archive", return_value=None),
            patch("apm_cli.deps.download_strategies.build_artifactory_archive_url") as mock_url,
        ):
            mock_url.return_value = ["https://art.example.com/url1"]
            with pytest.raises(RuntimeError, match="Failed to download file"):
                delegate.download_file_from_artifactory(
                    host="art.example.com",
                    prefix="artifactory/github",
                    owner="owner",
                    repo="repo",
                    file_path="missing_file.yml",
                    ref="main",
                )


# ===========================================================================
# DownloadDelegate — try_raw_download
# ===========================================================================


class TestTryRawDownload:
    """CDN raw download best-effort helper."""

    def test_returns_content_on_200(self) -> None:
        """Returns bytes when raw CDN returns 200."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        resp = _make_mock_response(200, content=b"raw content")
        with patch("apm_cli.deps.download_strategies.requests.get", return_value=resp):
            result = delegate.try_raw_download("owner", "repo", "main", "apm.yml")
        assert result == b"raw content"

    def test_returns_none_on_404(self) -> None:
        """Returns None when CDN returns 404."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        resp = _make_mock_response(404)
        with patch("apm_cli.deps.download_strategies.requests.get", return_value=resp):
            result = delegate.try_raw_download("owner", "repo", "main", "apm.yml")
        assert result is None

    def test_returns_none_on_request_exception(self) -> None:
        """Returns None when request raises an exception."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        with patch(
            "apm_cli.deps.download_strategies.requests.get",
            side_effect=requests.exceptions.ConnectionError("down"),
        ):
            result = delegate.try_raw_download("owner", "repo", "main", "apm.yml")
        assert result is None


# ===========================================================================
# DownloadDelegate — download_ado_file
# ===========================================================================


class TestDownloadAdoFile:
    """Azure DevOps file download."""

    def test_successful_download(self) -> None:
        """Downloads file successfully from ADO."""
        host = _make_host(ado_token="ado-pat")
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref(
            repo_url="myorg/MyProject/myrepo",
            host="dev.azure.com",
            ado_org="myorg",
            ado_project="MyProject",
            ado_repo="myrepo",
        )
        mock_resp = _make_mock_response(200, content=b"file data")
        host._resilient_get = MagicMock(return_value=mock_resp)

        with patch("apm_cli.deps.download_strategies.build_ado_api_url") as mock_url:
            mock_url.return_value = (
                "https://dev.azure.com/myorg/MyProject/_apis/git/repositories/myrepo/items"
            )
            result = delegate.download_ado_file(dep, "apm.yml", ref="main")
        assert result == b"file data"

    def test_raises_on_missing_ado_fields(self) -> None:
        """Raises ValueError when ADO fields are missing."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref(repo_url="owner/repo", host="dev.azure.com")  # missing ado_org etc.

        with pytest.raises(ValueError, match="Invalid Azure DevOps dependency reference"):
            delegate.download_ado_file(dep, "apm.yml", ref="main")

    def test_404_tries_fallback_branch(self) -> None:
        """On 404 with ref=main, retries with master."""
        host = _make_host(ado_token="ado-pat")
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref(
            repo_url="myorg/MyProject/myrepo",
            host="dev.azure.com",
            ado_org="myorg",
            ado_project="MyProject",
            ado_repo="myrepo",
        )
        not_found = _make_mock_response(404)
        success = _make_mock_response(200, content=b"from master")

        with patch("apm_cli.deps.download_strategies.build_ado_api_url") as mock_url:
            mock_url.return_value = (
                "https://dev.azure.com/myorg/MyProject/_apis/git/repositories/myrepo/items"
            )
            host._resilient_get = MagicMock(side_effect=[not_found, success])
            result = delegate.download_ado_file(dep, "apm.yml", ref="main")
        assert result == b"from master"

    def test_401_raises_runtime_error_with_token(self) -> None:
        """401 with ado_token set raises RuntimeError with permission message."""
        host = _make_host(ado_token="ado-pat")
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref(
            repo_url="myorg/MyProject/myrepo",
            host="dev.azure.com",
            ado_org="myorg",
            ado_project="MyProject",
            ado_repo="myrepo",
        )
        auth_failed = _make_mock_response(401)
        auth_http_err = requests.exceptions.HTTPError(response=auth_failed)
        auth_failed.raise_for_status.side_effect = auth_http_err

        with patch("apm_cli.deps.download_strategies.build_ado_api_url") as mock_url:
            mock_url.return_value = "https://dev.azure.com/myorg/MyProject/_apis"
            host._resilient_get = MagicMock(return_value=auth_failed)
            with pytest.raises(RuntimeError, match="Authentication failed"):
                delegate.download_ado_file(dep, "apm.yml", ref="main")

    def test_network_error_raises_runtime_error(self) -> None:
        """Network errors are wrapped in RuntimeError."""
        host = _make_host(ado_token="ado-pat")
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref(
            repo_url="myorg/MyProject/myrepo",
            host="dev.azure.com",
            ado_org="myorg",
            ado_project="MyProject",
            ado_repo="myrepo",
        )

        with patch("apm_cli.deps.download_strategies.build_ado_api_url") as mock_url:
            mock_url.return_value = "https://dev.azure.com/myorg/MyProject/_apis"
            host._resilient_get = MagicMock(
                side_effect=requests.exceptions.ConnectionError("network down")
            )
            with pytest.raises(RuntimeError, match="Network error"):
                delegate.download_ado_file(dep, "apm.yml", ref="main")


# ===========================================================================
# DownloadDelegate — download_gitlab_file
# ===========================================================================


class TestDownloadGitlabFile:
    """GitLab REST v4 file download."""

    def test_successful_download(self) -> None:
        """Downloads a file from GitLab successfully."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref(repo_url="group/project", host="gitlab.com")
        host.auth_resolver.classify_host.return_value = MagicMock(
            kind="gitlab",
            api_base="https://gitlab.com/api/v4",
        )
        host.auth_resolver.resolve.return_value = MagicMock(token="gl-token", source="env")
        mock_resp = _make_mock_response(200, content=b"gitlab file")
        host._resilient_get = MagicMock(return_value=mock_resp)

        result = delegate.download_gitlab_file(dep, "apm.yml", ref="main")
        assert result == b"gitlab file"

    def test_404_tries_master_fallback(self) -> None:
        """On 404 with ref=main, retries with master."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref(repo_url="group/project", host="gitlab.com")
        host.auth_resolver.classify_host.return_value = MagicMock(
            kind="gitlab",
            api_base="https://gitlab.com/api/v4",
        )
        host.auth_resolver.resolve.return_value = MagicMock(token="gl-token", source="env")
        not_found = _make_mock_response(404)
        success = _make_mock_response(200, content=b"from master")
        host._resilient_get = MagicMock(side_effect=[not_found, success])

        result = delegate.download_gitlab_file(dep, "apm.yml", ref="main")
        assert result == b"from master"

    def test_auth_error_raises_runtime_error_with_token(self) -> None:
        """401 with token raises RuntimeError."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref(repo_url="group/project", host="gitlab.com")
        host.auth_resolver.classify_host.return_value = MagicMock(
            kind="gitlab",
            api_base="https://gitlab.com/api/v4",
        )
        host.auth_resolver.resolve.return_value = MagicMock(token="gl-token", source="env")
        forbidden = _make_mock_response(401)
        forbidden_err = requests.exceptions.HTTPError(response=forbidden)
        forbidden.raise_for_status.side_effect = forbidden_err
        host._resilient_get = MagicMock(return_value=forbidden)

        with pytest.raises(RuntimeError, match="Authentication failed"):
            delegate.download_gitlab_file(dep, "apm.yml", ref="main")

    def test_verbose_callback_called_on_success(self) -> None:
        """verbose_callback is invoked when download succeeds."""
        host = _make_host()
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref(repo_url="group/project", host="gitlab.com")
        host.auth_resolver.classify_host.return_value = MagicMock(
            kind="gitlab",
            api_base="https://gitlab.com/api/v4",
        )
        host.auth_resolver.resolve.return_value = MagicMock(token="gl-token", source="env")
        mock_resp = _make_mock_response(200, content=b"data")
        host._resilient_get = MagicMock(return_value=mock_resp)
        calls = []

        delegate.download_gitlab_file(dep, "apm.yml", ref="main", verbose_callback=calls.append)
        assert any("Downloaded" in c for c in calls)


# ===========================================================================
# DownloadDelegate — download_github_file
# ===========================================================================


class TestDownloadGithubFile:
    """GitHub file download: CDN fast-path, Contents API, fallback branches."""

    def test_cdn_fast_path_used_for_public_github_without_token(self) -> None:
        """Uses raw.githubusercontent.com CDN for github.com without token."""
        host = _make_host(github_token=None)
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref("owner/repo", host="github.com")
        host.auth_resolver.resolve.return_value = MagicMock(token=None, source=None)

        with patch.object(delegate, "try_raw_download", return_value=b"cdn content") as mock_cdn:
            result = delegate.download_github_file(dep, "apm.yml", ref="main")

        assert result == b"cdn content"
        mock_cdn.assert_called_once_with("owner", "repo", "main", "apm.yml")

    def test_cdn_fallback_to_master_on_404(self) -> None:
        """Falls back to master when CDN returns None for main."""
        host = _make_host(github_token=None)
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref("owner/repo", host="github.com")
        host.auth_resolver.resolve.return_value = MagicMock(token=None, source=None)

        with patch.object(
            delegate, "try_raw_download", side_effect=[None, b"master content"]
        ) as mock_cdn:
            result = delegate.download_github_file(dep, "apm.yml", ref="main")

        assert result == b"master content"
        assert mock_cdn.call_count == 2

    def test_authenticated_request_skips_cdn(self) -> None:
        """Authenticated request goes directly to Contents API, not CDN."""
        host = _make_host(github_token="mytoken")
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref("owner/repo", host="github.com")
        host.auth_resolver.resolve.return_value = MagicMock(token="mytoken", source="env")
        ok_resp = _make_mock_response(200, content=b"api content")

        with (
            patch.object(delegate, "try_raw_download") as mock_cdn,
            patch.object(host, "_resilient_get", return_value=ok_resp),
        ):
            result = delegate.download_github_file(dep, "apm.yml", ref="main")

        mock_cdn.assert_not_called()
        assert result == b"api content"

    def test_fallback_to_master_on_api_404(self) -> None:
        """On Contents API 404 with ref=main, retries with master."""
        host = _make_host(github_token="tok")
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref("owner/repo", host="github.com")
        host.auth_resolver.resolve.return_value = MagicMock(token="tok", source="env")
        not_found = _make_mock_response(404)
        not_found_err = requests.exceptions.HTTPError(response=not_found)
        not_found.raise_for_status.side_effect = not_found_err
        ok_resp = _make_mock_response(200, content=b"master content")

        # First call → 404 (main); second call (fallback to master) → 200 success
        with patch.object(host, "_resilient_get", side_effect=[not_found, ok_resp]):
            result = delegate.download_github_file(dep, "apm.yml", ref="main")

        assert result == b"master content"

    def test_rate_limit_error_message_without_token(self) -> None:
        """Rate-limit error message instructs user to set GITHUB_APM_PAT."""
        host = _make_host(github_token=None)
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref("owner/repo", host="github.com")
        host.auth_resolver.resolve.return_value = MagicMock(token=None, source=None)
        rate_limited = _make_mock_response(403, headers={"X-RateLimit-Remaining": "0"})
        rate_limited_err = requests.exceptions.HTTPError(response=rate_limited)
        rate_limited.raise_for_status.side_effect = rate_limited_err

        with (
            patch.object(delegate, "try_raw_download", return_value=None),
            patch.object(host, "_resilient_get", return_value=rate_limited),
        ):
            with pytest.raises(RuntimeError, match="rate limit"):
                delegate.download_github_file(dep, "apm.yml", ref="main")

    def test_network_error_wrapped_in_runtime_error(self) -> None:
        """ConnectionError is wrapped in RuntimeError."""
        host = _make_host(github_token="tok")
        delegate = DownloadDelegate(host)
        dep = _make_dep_ref("owner/repo", host="github.com")
        host.auth_resolver.resolve.return_value = MagicMock(token="tok", source="env")

        with patch.object(
            host,
            "_resilient_get",
            side_effect=requests.exceptions.ConnectionError("connection down"),
        ):
            with pytest.raises(RuntimeError, match="Network error"):
                delegate.download_github_file(dep, "apm.yml", ref="main")


# ===========================================================================
# DownloadDelegate — _is_configured_ghes
# ===========================================================================


class TestIsConfiguredGhes:
    """GITHUB_HOST env-var opt-in for custom GHES domains."""

    def test_returns_true_when_host_matches_env(self) -> None:
        """Returns True when host matches GITHUB_HOST env var."""
        with patch.dict(os.environ, {"GITHUB_HOST": "ghes.company.com"}):
            assert DownloadDelegate._is_configured_ghes("ghes.company.com") is True

    def test_returns_false_when_host_differs(self) -> None:
        """Returns False when host does not match GITHUB_HOST."""
        with patch.dict(os.environ, {"GITHUB_HOST": "other.example.com"}):
            assert DownloadDelegate._is_configured_ghes("ghes.company.com") is False

    def test_returns_false_when_env_not_set(self) -> None:
        """Returns False when GITHUB_HOST env var is absent."""
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_HOST"}
        with patch.dict(os.environ, env, clear=True):
            assert DownloadDelegate._is_configured_ghes("ghes.company.com") is False

    def test_case_insensitive_match(self) -> None:
        """Case-insensitive host comparison."""
        with patch.dict(os.environ, {"GITHUB_HOST": "GHES.COMPANY.COM"}):
            assert DownloadDelegate._is_configured_ghes("ghes.company.com") is True


# ===========================================================================
# DownloadDelegate — _build_contents_api_urls
# ===========================================================================


class TestBuildContentsApiUrls:
    """Contents API URL construction for different host types."""

    def test_github_com_uses_api_github_com(self) -> None:
        """github.com yields api.github.com URL."""
        from urllib.parse import urlparse

        urls = DownloadDelegate._build_contents_api_urls(
            "github.com", "owner", "repo", "apm.yml", "main"
        )
        assert urls
        parsed = urlparse(urls[0])
        assert "api.github.com" in parsed.netloc

    def test_ghe_com_uses_v3_api(self) -> None:
        """*.ghe.com yields /api/v3 endpoint."""

        urls = DownloadDelegate._build_contents_api_urls(
            "myorg.ghe.com", "owner", "repo", "apm.yml", "main"
        )
        assert urls
        assert any("/api/v3/" in u for u in urls)

    def test_generic_host_produces_url(self) -> None:
        """Generic host (non-GitHub) produces a URL."""
        urls = DownloadDelegate._build_contents_api_urls(
            "gitea.example.com", "owner", "repo", "apm.yml", "main", is_github_host=False
        )
        assert urls
        assert all("gitea.example.com" in u for u in urls)

    def test_ghes_host_uses_v3_api(self) -> None:
        """Configured GHES uses /api/v3/ path."""
        with patch.dict(os.environ, {"GITHUB_HOST": "ghes.corp.com"}):
            urls = DownloadDelegate._build_contents_api_urls(
                "ghes.corp.com", "owner", "repo", "apm.yml", "main"
            )
        assert urls
        assert any("/api/v3/" in u for u in urls)


# ===========================================================================
# DownloadDelegate — _build_generic_host_auth_headers
# ===========================================================================


class TestBuildGenericHostAuthHeaders:
    """Auth header construction with security guard for generic hosts."""

    def test_git_credential_fill_source_attaches_token(self) -> None:
        """git-credential-fill sourced token is forwarded."""
        auth_ctx = MagicMock(token="cred-token", source="git-credential-fill")
        headers = DownloadDelegate._build_generic_host_auth_headers(
            "gitea.example.com", auth_ctx, accept="application/json"
        )
        assert headers.get("Authorization") == "token cred-token"
        assert headers.get("Accept") == "application/json"

    def test_global_github_token_not_forwarded(self) -> None:
        """Global GITHUB_TOKEN is NOT forwarded to generic hosts."""
        auth_ctx = MagicMock(token="gh-token", source="GITHUB_TOKEN")
        headers = DownloadDelegate._build_generic_host_auth_headers("gitea.example.com", auth_ctx)
        assert "Authorization" not in headers

    def test_org_scoped_pat_forwarded(self) -> None:
        """GITHUB_APM_PAT_<ORG> token is forwarded (explicit opt-in)."""
        auth_ctx = MagicMock(token="org-pat", source="GITHUB_APM_PAT_MYORG")
        headers = DownloadDelegate._build_generic_host_auth_headers("myorg.example.com", auth_ctx)
        assert headers.get("Authorization") == "token org-pat"

    def test_no_token_returns_empty_auth(self) -> None:
        """Absent token returns headers without Authorization."""
        auth_ctx = MagicMock(token=None, source=None)
        headers = DownloadDelegate._build_generic_host_auth_headers(
            "gitea.example.com", auth_ctx, accept="application/json"
        )
        assert "Authorization" not in headers
        assert headers.get("Accept") == "application/json"

    def test_configured_ghes_forwards_token(self) -> None:
        """GITHUB_HOST-configured GHES forwards the token."""
        auth_ctx = MagicMock(token="ghes-token", source="GITHUB_TOKEN")
        with patch.dict(os.environ, {"GITHUB_HOST": "ghes.corp.com"}):
            headers = DownloadDelegate._build_generic_host_auth_headers("ghes.corp.com", auth_ctx)
        assert headers.get("Authorization") == "token ghes-token"


# ===========================================================================
# DownloadDelegate — _extract_contents_api_payload
# ===========================================================================


class TestExtractContentsApiPayload:
    """Payload extraction for GitHub (raw) and generic (JSON envelope) responses."""

    def test_github_host_returns_raw_content(self) -> None:
        """GitHub family returns response.content directly."""
        resp = MagicMock()
        resp.content = b"raw bytes"
        result = DownloadDelegate._extract_contents_api_payload(resp, is_github_host=True)
        assert result == b"raw bytes"

    def test_generic_host_decodes_base64_json_envelope(self) -> None:
        """Generic host decodes base64-encoded JSON envelope."""
        raw_content = b"file content"
        encoded = base64.b64encode(raw_content).decode()
        payload = json.dumps({"content": encoded, "encoding": "base64"}).encode()
        resp = MagicMock()
        resp.content = payload
        resp.headers = {"Content-Type": "application/json"}
        result = DownloadDelegate._extract_contents_api_payload(resp, is_github_host=False)
        assert result == raw_content

    def test_generic_host_falls_back_when_no_json(self) -> None:
        """Non-JSON body returned as-is from generic host."""
        resp = MagicMock()
        resp.content = b"plain bytes"
        resp.headers = {"Content-Type": "text/plain"}
        result = DownloadDelegate._extract_contents_api_payload(resp, is_github_host=False)
        assert result == b"plain bytes"

    def test_generic_host_json_without_content_key(self) -> None:
        """JSON without 'content' key returns raw body."""
        payload = json.dumps({"sha": "abc123"}).encode()
        resp = MagicMock()
        resp.content = payload
        resp.headers = {"Content-Type": "application/json"}
        result = DownloadDelegate._extract_contents_api_payload(resp, is_github_host=False)
        assert result == payload

    def test_generic_host_non_base64_encoding_returns_string_bytes(self) -> None:
        """Non-base64 encoding returns content field as UTF-8 bytes."""
        payload = json.dumps({"content": "hello world", "encoding": ""}).encode()
        resp = MagicMock()
        resp.content = payload
        resp.headers = {"Content-Type": "application/json"}
        result = DownloadDelegate._extract_contents_api_payload(resp, is_github_host=False)
        assert result == b"hello world"


# ===========================================================================
# DownloadDelegate — _build_unsupported_or_missing_error
# ===========================================================================


class TestBuildUnsupportedOrMissingError:
    """Error message construction for missing files."""

    def test_github_host_simple_message(self) -> None:
        """GitHub error message is concise."""
        msg = DownloadDelegate._build_unsupported_or_missing_error(
            "github.com",
            "owner/repo",
            "apm.yml",
            "main",
            ["https://api.github.com/repos/owner/repo/contents/apm.yml?ref=main"],
            is_github_host=True,
        )
        assert "apm.yml" in msg
        assert "owner/repo" in msg

    def test_generic_host_includes_tried_families(self) -> None:
        """Generic host error names the URL families tried."""
        msg = DownloadDelegate._build_unsupported_or_missing_error(
            "gitea.example.com",
            "owner/repo",
            "apm.yml",
            "main",
            ["https://gitea.example.com/api/v1/repos/owner/repo/contents/apm.yml"],
            is_github_host=False,
        )
        assert "gitea.example.com" in msg
        assert "apm.yml" in msg

    def test_fallback_ref_included_in_message(self) -> None:
        """When fallback_ref is provided, message includes both refs."""
        msg = DownloadDelegate._build_unsupported_or_missing_error(
            "github.com",
            "owner/repo",
            "apm.yml",
            "main",
            ["https://api.github.com/repos/owner/repo/contents/apm.yml"],
            is_github_host=True,
            fallback_ref="master",
        )
        assert "main" in msg
        assert "master" in msg


# ===========================================================================
# CopilotClientAdapter — module-level pure helpers
# ===========================================================================


class TestCopilotPureHelpers:
    """Module-level helpers: _translate_env_placeholder, _extract_legacy_angle_vars, etc."""

    def test_translate_legacy_angle_syntax(self) -> None:
        assert _translate_env_placeholder("<MY_TOKEN>") == "${MY_TOKEN}"

    def test_translate_posix_dollar_brace(self) -> None:
        assert _translate_env_placeholder("${MY_TOKEN}") == "${MY_TOKEN}"

    def test_translate_vscode_env_prefix(self) -> None:
        assert _translate_env_placeholder("${env:MY_TOKEN}") == "${MY_TOKEN}"

    def test_translate_mixed_placeholders(self) -> None:
        result = _translate_env_placeholder("a=<A> b=${B} c=${env:C}")
        assert result == "a=${A} b=${B} c=${C}"

    def test_translate_non_string_passthrough(self) -> None:
        assert _translate_env_placeholder(42) == 42
        assert _translate_env_placeholder(None) is None
        assert _translate_env_placeholder(True) is True

    def test_translate_idempotent(self) -> None:
        """Applying translation twice is same as once."""
        original = "<TOKEN> ${env:KEY}"
        first = _translate_env_placeholder(original)
        second = _translate_env_placeholder(first)
        assert first == second

    def test_extract_legacy_angle_vars_finds_names(self) -> None:
        result = _extract_legacy_angle_vars("host=<HOST> token=<TOKEN>")
        assert result == {"HOST", "TOKEN"}

    def test_extract_legacy_angle_vars_empty_for_non_string(self) -> None:
        assert _extract_legacy_angle_vars(None) == set()
        assert _extract_legacy_angle_vars(123) == set()

    def test_extract_legacy_angle_vars_none_in_clean_string(self) -> None:
        assert _extract_legacy_angle_vars("no placeholders here") == set()

    def test_has_env_placeholder_detects_angle(self) -> None:
        assert _has_env_placeholder("<MY_VAR>") is True

    def test_has_env_placeholder_detects_dollar_brace(self) -> None:
        assert _has_env_placeholder("${MY_VAR}") is True

    def test_has_env_placeholder_detects_env_prefix(self) -> None:
        assert _has_env_placeholder("${env:MY_VAR}") is True

    def test_has_env_placeholder_false_for_plain_string(self) -> None:
        assert _has_env_placeholder("just a literal") is False

    def test_has_env_placeholder_false_for_non_string(self) -> None:
        assert _has_env_placeholder(42) is False

    def test_stringify_env_literal_bool_true(self) -> None:
        assert _stringify_env_literal(True) == "true"

    def test_stringify_env_literal_bool_false(self) -> None:
        assert _stringify_env_literal(False) == "false"

    def test_stringify_env_literal_int(self) -> None:
        assert _stringify_env_literal(1) == "1"

    def test_stringify_env_literal_string(self) -> None:
        assert _stringify_env_literal("hello") == "hello"


# ===========================================================================
# CopilotClientAdapter — config path and basic I/O
# ===========================================================================


class TestCopilotConfigPath:
    """Config path resolution and I/O primitives."""

    def test_get_config_path_returns_copilot_dir(self) -> None:
        """get_config_path returns ~/.copilot/mcp-config.json."""
        adapter = CopilotClientAdapter()
        fake_home = Path("/fake/home")
        with patch("apm_cli.adapters.client.copilot.Path.home", return_value=fake_home):
            path = adapter.get_config_path()
        assert path.endswith("mcp-config.json")
        assert ".copilot" in path

    def test_get_current_config_returns_empty_dict_when_no_file(self) -> None:
        """get_current_config returns {} when file doesn't exist."""
        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value="/nonexistent/mcp-config.json"):
            config = adapter.get_current_config()
        assert config == {}

    def test_get_current_config_parses_existing_json(self, tmp_path: Path) -> None:
        """get_current_config reads and parses existing config."""
        config_file = tmp_path / "mcp-config.json"
        config_file.write_text(json.dumps({"mcpServers": {"my-server": {"type": "http"}}}))
        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            config = adapter.get_current_config()
        assert "mcpServers" in config
        assert "my-server" in config["mcpServers"]

    def test_get_current_config_returns_empty_on_invalid_json(self, tmp_path: Path) -> None:
        """get_current_config returns {} on malformed JSON."""
        config_file = tmp_path / "mcp-config.json"
        config_file.write_text("{ not valid json }")
        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            config = adapter.get_current_config()
        assert config == {}

    def test_update_config_writes_mcpservers(self, tmp_path: Path) -> None:
        """update_config persists mcpServers entry to disk."""
        config_path = tmp_path / ".copilot" / "mcp-config.json"
        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value=str(config_path)):
            adapter.update_config({"my-server": {"type": "http", "url": "https://example.com"}})
        saved = json.loads(config_path.read_text())
        assert saved["mcpServers"]["my-server"]["type"] == "http"

    def test_update_config_merges_into_existing(self, tmp_path: Path) -> None:
        """update_config merges with existing mcpServers entries."""
        config_path = tmp_path / ".copilot" / "mcp-config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"mcpServers": {"existing": {"type": "local"}}}))
        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value=str(config_path)):
            adapter.update_config({"new-server": {"type": "http"}})
        saved = json.loads(config_path.read_text())
        assert "existing" in saved["mcpServers"]
        assert "new-server" in saved["mcpServers"]


# ===========================================================================
# CopilotClientAdapter — _select_best_package
# ===========================================================================


class TestSelectBestPackage:
    """Package priority selection logic."""

    def _make_adapter(self) -> CopilotClientAdapter:
        return CopilotClientAdapter()

    def test_prefers_npm_over_docker(self) -> None:
        """npm package wins over docker when both available."""
        adapter = self._make_adapter()
        packages = [
            {"name": "docker/image", "registry_name": "docker"},
            {"name": "@scope/pkg", "registry_name": "npm"},
        ]
        result = adapter._select_best_package(packages)
        assert result["registry_name"] == "npm"

    def test_prefers_docker_over_pypi(self) -> None:
        """docker package wins over pypi."""
        adapter = self._make_adapter()
        packages = [
            {"name": "mypkg", "registry_name": "pypi"},
            {"name": "docker/image", "registry_name": "docker"},
        ]
        result = adapter._select_best_package(packages)
        assert result["registry_name"] == "docker"

    def test_returns_first_when_no_priority_match(self) -> None:
        """Returns first package when none match priority list."""
        adapter = self._make_adapter()
        packages = [{"name": "pkg1", "registry_name": "custom"}]
        result = adapter._select_best_package(packages)
        assert result["name"] == "pkg1"

    def test_returns_none_for_empty_list(self) -> None:
        """Returns None for empty package list."""
        adapter = self._make_adapter()
        assert adapter._select_best_package([]) is None


# ===========================================================================
# CopilotClientAdapter — _select_remote_with_url
# ===========================================================================


class TestSelectRemoteWithUrl:
    """Select the first remote with a usable URL."""

    def test_returns_first_with_url(self) -> None:
        remotes = [{"url": ""}, {"url": "https://example.com/mcp"}]
        result = CopilotClientAdapter._select_remote_with_url(remotes)
        assert result["url"] == "https://example.com/mcp"

    def test_returns_none_when_all_empty(self) -> None:
        remotes = [{"url": ""}, {"url": "  "}]
        result = CopilotClientAdapter._select_remote_with_url(remotes)
        assert result is None

    def test_returns_first_valid(self) -> None:
        remotes = [{"url": "https://first.example.com"}, {"url": "https://second.example.com"}]
        result = CopilotClientAdapter._select_remote_with_url(remotes)
        assert result["url"] == "https://first.example.com"


# ===========================================================================
# CopilotClientAdapter — _resolve_environment_variables (translate mode)
# ===========================================================================


class TestResolveEnvironmentVariables:
    """Environment variable resolution in translate (runtime substitution) mode."""

    def _make_adapter(self) -> CopilotClientAdapter:
        adapter = CopilotClientAdapter()
        adapter._last_env_placeholder_keys = set()
        adapter._last_legacy_angle_vars = set()
        return adapter

    def test_list_form_produces_runtime_placeholders(self) -> None:
        """Required descriptors become ${NAME}; optional ones without an
        observed value are omitted (honoring optional registry inputs, #1734)."""
        adapter = self._make_adapter()
        env_vars = [
            {"name": "GITHUB_TOKEN", "description": "GitHub token", "required": True},
            {"name": "API_KEY", "description": "API key", "required": False},
        ]
        result = adapter._resolve_environment_variables(env_vars)
        assert result["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"
        assert "API_KEY" not in result

    def test_github_toolsets_preserved_as_literal(self) -> None:
        """GITHUB_TOOLSETS default stays literal (non-secret)."""
        adapter = self._make_adapter()
        env_vars = [{"name": "GITHUB_TOOLSETS", "description": "Toolsets", "required": False}]
        result = adapter._resolve_environment_variables(env_vars)
        assert result["GITHUB_TOOLSETS"] == "context"

    def test_github_dynamic_toolsets_preserved_as_literal(self) -> None:
        """GITHUB_DYNAMIC_TOOLSETS default stays literal."""
        adapter = self._make_adapter()
        env_vars = [
            {
                "name": "GITHUB_DYNAMIC_TOOLSETS",
                "description": "Dynamic toolsets",
                "required": False,
            }
        ]
        result = adapter._resolve_environment_variables(env_vars)
        assert result["GITHUB_DYNAMIC_TOOLSETS"] == "1"

    def test_dict_form_translates_placeholders(self) -> None:
        """Dict-form env with placeholder values are translated."""
        adapter = self._make_adapter()
        env_vars = {"MY_TOKEN": "<MY_TOKEN>", "OTHER_VAR": "${env:OTHER_VAR}"}
        result = adapter._resolve_environment_variables(env_vars)
        assert result["MY_TOKEN"] == "${MY_TOKEN}"
        assert result["OTHER_VAR"] == "${OTHER_VAR}"

    def test_dict_form_literal_value_becomes_placeholder(self) -> None:
        """Dict-form literal value is replaced with ${NAME} placeholder."""
        adapter = self._make_adapter()
        env_vars = {"SECRET_KEY": "hardcoded_secret"}
        result = adapter._resolve_environment_variables(env_vars)
        assert result["SECRET_KEY"] == "${SECRET_KEY}"

    def test_dict_form_github_literal_default_preserved(self) -> None:
        """Dict-form GITHUB_TOOLSETS=context stays literal."""
        adapter = self._make_adapter()
        env_vars = {"GITHUB_TOOLSETS": "context"}
        result = adapter._resolve_environment_variables(env_vars)
        assert result["GITHUB_TOOLSETS"] == "context"

    def test_dict_form_skips_none_values(self) -> None:
        """Dict-form None values are excluded from output."""
        adapter = self._make_adapter()
        env_vars = {"MY_VAR": None, "OTHER": "value"}
        result = adapter._resolve_environment_variables(env_vars)
        assert "MY_VAR" not in result

    def test_dict_form_bool_stringified(self) -> None:
        """Dict-form bool values are stringified."""
        adapter = self._make_adapter()
        env_vars = {"FEATURE_FLAG": True}
        result = adapter._resolve_environment_variables(env_vars)
        assert result["FEATURE_FLAG"] == "true"

    def test_legacy_angle_vars_tracked(self) -> None:
        """Legacy <VAR> syntax is tracked in _last_legacy_angle_vars."""
        adapter = self._make_adapter()
        env_vars = {"MY_TOKEN": "<MY_TOKEN>"}
        adapter._resolve_environment_variables(env_vars)
        assert "MY_TOKEN" in adapter._last_legacy_angle_vars


# ===========================================================================
# CopilotClientAdapter — _resolve_variable_placeholders
# ===========================================================================


class TestResolveVariablePlaceholders:
    """Placeholder resolution in translate mode."""

    def _make_adapter(self) -> CopilotClientAdapter:
        adapter = CopilotClientAdapter()
        adapter._last_legacy_angle_vars = set()
        adapter._last_env_placeholder_keys = set()
        return adapter

    def test_legacy_angle_translated(self) -> None:
        adapter = self._make_adapter()
        result = adapter._resolve_variable_placeholders("<MY_VAR>", {}, {})
        assert result == "${MY_VAR}"

    def test_dollar_brace_passthrough(self) -> None:
        adapter = self._make_adapter()
        result = adapter._resolve_variable_placeholders("${ALREADY_GOOD}", {}, {})
        assert result == "${ALREADY_GOOD}"

    def test_runtime_vars_substituted(self) -> None:
        """APM {runtime_var} placeholders are resolved at install time."""
        adapter = self._make_adapter()
        result = adapter._resolve_variable_placeholders("--repo={repo}", {}, {"repo": "owner/repo"})
        assert result == "--repo=owner/repo"

    def test_runtime_vars_not_confused_with_env_placeholders(self) -> None:
        """${VAR} is NOT treated as a runtime_var (different syntax)."""
        adapter = self._make_adapter()
        result = adapter._resolve_variable_placeholders(
            "${ENV_VAR} and {runtime}", {}, {"runtime": "val"}
        )
        assert "${ENV_VAR}" in result
        assert "val" in result

    def test_empty_string_returned_as_is(self) -> None:
        adapter = self._make_adapter()
        result = adapter._resolve_variable_placeholders("", {}, {})
        assert result == ""

    def test_no_placeholders_passthrough(self) -> None:
        adapter = self._make_adapter()
        result = adapter._resolve_variable_placeholders("npx --yes @scope/pkg", {}, {})
        assert result == "npx --yes @scope/pkg"


# ===========================================================================
# CopilotClientAdapter — _process_arguments
# ===========================================================================


class TestProcessArguments:
    """Argument processing from registry argument descriptor objects."""

    def _make_adapter(self) -> CopilotClientAdapter:
        adapter = CopilotClientAdapter()
        adapter._last_legacy_angle_vars = set()
        adapter._last_env_placeholder_keys = set()
        return adapter

    def test_positional_argument_extracted(self) -> None:
        """Positional argument value is extracted."""
        adapter = self._make_adapter()
        args = [{"type": "positional", "value": "--verbose"}]
        result = adapter._process_arguments(args, {}, {})
        assert "--verbose" in result

    def test_named_argument_flag_and_value(self) -> None:
        """Named argument with distinct name/value produces both flag and value."""
        adapter = self._make_adapter()
        args = [{"type": "named", "name": "--config", "value": "path/to/config"}]
        result = adapter._process_arguments(args, {}, {})
        assert "--config" in result
        assert "path/to/config" in result

    def test_named_argument_same_name_value_no_duplicate(self) -> None:
        """Named arg where value == name (flag) does not duplicate."""
        adapter = self._make_adapter()
        args = [{"type": "named", "name": "--verbose", "value": "--verbose"}]
        result = adapter._process_arguments(args, {}, {})
        assert result.count("--verbose") == 1

    def test_string_argument_processed(self) -> None:
        """String argument is processed and added directly."""
        adapter = self._make_adapter()
        args = ["--some-flag", "value"]
        result = adapter._process_arguments(args, {}, {})
        assert "--some-flag" in result
        assert "value" in result

    def test_empty_arguments_list(self) -> None:
        adapter = self._make_adapter()
        result = adapter._process_arguments([], {}, {})
        assert result == []

    def test_runtime_var_resolved_in_positional(self) -> None:
        """Runtime var in positional arg value is substituted."""
        adapter = self._make_adapter()
        args = [{"type": "positional", "value": "--repo={repo}"}]
        result = adapter._process_arguments(args, {}, {"repo": "owner/repo"})
        assert "--repo=owner/repo" in result


# ===========================================================================
# CopilotClientAdapter — _inject_env_vars_into_docker_args
# ===========================================================================


class TestInjectEnvVarsIntoDockerArgs:
    """Docker arg injection including -i and --rm guarantees."""

    def _make_adapter(self) -> CopilotClientAdapter:
        return CopilotClientAdapter()

    def test_adds_interactive_flag_when_missing(self) -> None:
        """Injects -i flag when not present."""
        adapter = self._make_adapter()
        result = adapter._inject_env_vars_into_docker_args(
            ["run", "--rm", "myimage"], {"TOKEN": "${TOKEN}"}
        )
        assert "-i" in result

    def test_adds_rm_flag_when_missing(self) -> None:
        """Injects --rm flag when not present."""
        adapter = self._make_adapter()
        result = adapter._inject_env_vars_into_docker_args(
            ["run", "-i", "myimage"], {"TOKEN": "${TOKEN}"}
        )
        assert "--rm" in result

    def test_does_not_duplicate_existing_flags(self) -> None:
        """Existing -i and --rm are not duplicated."""
        adapter = self._make_adapter()
        result = adapter._inject_env_vars_into_docker_args(["run", "-i", "--rm", "myimage"], {})
        assert result.count("-i") == 1
        assert result.count("--rm") == 1

    def test_env_var_placeholder_replaced(self) -> None:
        """Env var name in args is replaced with -e KEY=VALUE."""
        adapter = self._make_adapter()
        result = adapter._inject_env_vars_into_docker_args(
            ["run", "--rm", "-i", "MY_TOKEN", "myimage"], {"MY_TOKEN": "${MY_TOKEN}"}
        )
        assert "-e" in result
        assert any("MY_TOKEN" in a for a in result)


# ===========================================================================
# CopilotClientAdapter — _dispatch_package_to_config
# ===========================================================================


class TestDispatchPackageToConfig:
    """Package-type dispatch into config structure."""

    def _make_adapter(self) -> CopilotClientAdapter:
        adapter = CopilotClientAdapter()
        adapter._last_legacy_angle_vars = set()
        adapter._last_env_placeholder_keys = set()
        return adapter

    def test_npm_package_sets_npx_command(self) -> None:
        """npm package produces npx command."""
        adapter = self._make_adapter()
        config: dict = {}
        adapter._dispatch_package_to_config(
            config=config,
            package_name="@scope/my-mcp-server",
            registry_name="npm",
            runtime_hint="",
            processed_runtime_args=[],
            processed_package_args=[],
            resolved_env={},
        )
        assert config["command"] == "npx"
        assert "@scope/my-mcp-server" in config["args"]

    def test_npm_package_with_runtime_hint(self) -> None:
        """Custom runtime_hint replaces default npx."""
        adapter = self._make_adapter()
        config: dict = {}
        adapter._dispatch_package_to_config(
            config=config,
            package_name="my-pkg",
            registry_name="npm",
            runtime_hint="bunx",
            processed_runtime_args=[],
            processed_package_args=[],
            resolved_env={},
        )
        assert config["command"] == "bunx"

    def test_docker_package_sets_docker_command(self) -> None:
        """docker package produces docker command."""
        adapter = self._make_adapter()
        config: dict = {}
        adapter._dispatch_package_to_config(
            config=config,
            package_name="myorg/myimage:latest",
            registry_name="docker",
            runtime_hint="",
            processed_runtime_args=[],
            processed_package_args=[],
            resolved_env={},
        )
        assert config["command"] == "docker"
        assert "run" in config["args"]

    def test_npm_includes_env_when_present(self) -> None:
        """npm dispatch includes env block when env vars resolved."""
        adapter = self._make_adapter()
        config: dict = {}
        adapter._dispatch_package_to_config(
            config=config,
            package_name="@scope/pkg",
            registry_name="npm",
            runtime_hint="",
            processed_runtime_args=[],
            processed_package_args=[],
            resolved_env={"MY_TOKEN": "${MY_TOKEN}"},
        )
        assert config.get("env") == {"MY_TOKEN": "${MY_TOKEN}"}

    def test_npm_omits_env_when_empty(self) -> None:
        """npm dispatch omits env block when no env vars."""
        adapter = self._make_adapter()
        config: dict = {}
        adapter._dispatch_package_to_config(
            config=config,
            package_name="@scope/pkg",
            registry_name="npm",
            runtime_hint="",
            processed_runtime_args=[],
            processed_package_args=[],
            resolved_env={},
        )
        assert "env" not in config


# ===========================================================================
# CopilotClientAdapter — _format_server_config
# ===========================================================================


class TestFormatServerConfig:
    """Server config formatting for remote, package, and raw-stdio servers."""

    def _make_adapter(self) -> CopilotClientAdapter:
        adapter = CopilotClientAdapter()
        adapter._last_legacy_angle_vars = set()
        adapter._last_env_placeholder_keys = set()
        return adapter

    def test_npm_package_config(self) -> None:
        """npm package server produces type=local with npx command."""
        adapter = self._make_adapter()
        server_info = {
            "id": "uuid-123",
            "name": "my-server",
            "packages": [
                {
                    "name": "@scope/my-server",
                    "registry_name": "npm",
                    "environment_variables": [],
                    "runtime_arguments": [],
                    "package_arguments": [],
                }
            ],
        }
        config = adapter._format_server_config(server_info)
        assert config["type"] == "local"
        assert config["command"] == "npx"
        assert "@scope/my-server" in config["args"]
        assert config["id"] == "uuid-123"

    def test_remote_server_config(self) -> None:
        """Remote server produces type=http with URL."""
        from urllib.parse import urlparse

        adapter = self._make_adapter()
        server_info = {
            "id": "uuid-456",
            "name": "remote-server",
            "remotes": [
                {
                    "url": "https://api.example.com/mcp",
                    "transport_type": "http",
                    "headers": [],
                }
            ],
        }
        config = adapter._format_server_config(server_info)
        assert config["type"] == "http"
        parsed = urlparse(config["url"])
        assert parsed.scheme == "https"
        assert parsed.netloc == "api.example.com"

    def test_raw_stdio_server_config(self) -> None:
        """Self-defined stdio server uses command/args directly."""
        adapter = self._make_adapter()
        server_info = {
            "id": "uuid-789",
            "name": "stdio-server",
            "_raw_stdio": {
                "command": "python",
                "args": ["-m", "my_server"],
                "env": {},
            },
        }
        config = adapter._format_server_config(server_info)
        assert config["command"] == "python"
        assert "-m" in config["args"]

    def test_raises_for_empty_packages_and_no_remotes(self) -> None:
        """ValueError raised when server has neither packages nor remotes."""
        adapter = self._make_adapter()
        server_info = {"id": "uuid-000", "name": "broken-server", "packages": [], "remotes": []}
        with pytest.raises(ValueError, match="incomplete configuration"):
            adapter._format_server_config(server_info)

    def test_tools_override_applied(self) -> None:
        """_apm_tools_override replaces default ['*'] tools."""
        adapter = self._make_adapter()
        server_info = {
            "id": "uuid-tools",
            "name": "my-server",
            "packages": [
                {
                    "name": "@scope/my-server",
                    "registry_name": "npm",
                    "environment_variables": [],
                    "runtime_arguments": [],
                    "package_arguments": [],
                }
            ],
            "_apm_tools_override": ["tool1", "tool2"],
        }
        config = adapter._format_server_config(server_info)
        assert config["tools"] == ["tool1", "tool2"]

    def test_invalid_transport_type_raises(self) -> None:
        """Unsupported remote transport_type raises ValueError."""
        adapter = self._make_adapter()
        server_info = {
            "id": "uuid-bad",
            "name": "grpc-server",
            "remotes": [
                {"url": "https://example.com/mcp", "transport_type": "grpc", "headers": []}
            ],
        }
        with pytest.raises(ValueError, match="Unsupported remote transport"):
            adapter._format_server_config(server_info)

    def test_sse_transport_accepted(self) -> None:
        """SSE transport_type is accepted."""
        adapter = self._make_adapter()
        server_info = {
            "id": "uuid-sse",
            "name": "sse-server",
            "remotes": [{"url": "https://example.com/sse", "transport_type": "sse", "headers": []}],
        }
        config = adapter._format_server_config(server_info)
        assert config["type"] == "http"

    def test_missing_transport_defaults_to_http(self) -> None:
        """Missing transport_type defaults to http."""
        adapter = self._make_adapter()
        server_info = {
            "id": "uuid-default",
            "name": "default-server",
            "remotes": [{"url": "https://example.com/mcp", "headers": []}],
        }
        config = adapter._format_server_config(server_info)
        assert config["type"] == "http"


# ===========================================================================
# CopilotClientAdapter — configure_mcp_server
# ===========================================================================


class TestConfigureMcpServer:
    """configure_mcp_server end-to-end flow."""

    def _make_adapter(self, tmp_path: Path) -> CopilotClientAdapter:
        adapter = CopilotClientAdapter()
        config_path = tmp_path / ".copilot" / "mcp-config.json"
        adapter.get_config_path = lambda: str(config_path)  # type: ignore[method-assign]
        CopilotClientAdapter.reset_install_run_state()
        return adapter

    def test_returns_false_for_empty_server_url(self, tmp_path: Path) -> None:
        """Returns False when server_url is empty."""
        adapter = self._make_adapter(tmp_path)
        result = adapter.configure_mcp_server("")
        assert result is False

    def test_returns_false_when_server_info_not_found(self, tmp_path: Path) -> None:
        """Returns False when _fetch_server_info returns None."""
        adapter = self._make_adapter(tmp_path)
        with patch.object(adapter, "_fetch_server_info", return_value=None):
            result = adapter.configure_mcp_server("owner/my-server")
        assert result is False

    def test_returns_true_on_successful_install(self, tmp_path: Path) -> None:
        """Returns True when server is successfully configured."""
        adapter = self._make_adapter(tmp_path)
        server_info = {
            "id": "uuid-123",
            "name": "my-server",
            "packages": [
                {
                    "name": "@scope/my-server",
                    "registry_name": "npm",
                    "environment_variables": [],
                    "runtime_arguments": [],
                    "package_arguments": [],
                }
            ],
        }
        with patch.object(adapter, "_fetch_server_info", return_value=server_info):
            result = adapter.configure_mcp_server("owner/my-server")
        assert result is True

    def test_config_key_derived_from_slash_url(self, tmp_path: Path) -> None:
        """Config key is the last path component of owner/server-name URL."""
        adapter = self._make_adapter(tmp_path)
        server_info = {
            "id": "uuid-123",
            "name": "my-server",
            "packages": [
                {
                    "name": "@scope/my-server",
                    "registry_name": "npm",
                    "environment_variables": [],
                    "runtime_arguments": [],
                    "package_arguments": [],
                }
            ],
        }
        with patch.object(adapter, "_fetch_server_info", return_value=server_info):
            adapter.configure_mcp_server("owner/my-server")
        config = adapter.get_current_config()
        assert "my-server" in config.get("mcpServers", {})

    def test_explicit_server_name_overrides_derived_key(self, tmp_path: Path) -> None:
        """Explicit server_name is used as config key."""
        adapter = self._make_adapter(tmp_path)
        server_info = {
            "id": "uuid-123",
            "name": "original-name",
            "packages": [
                {
                    "name": "@scope/pkg",
                    "registry_name": "npm",
                    "environment_variables": [],
                    "runtime_arguments": [],
                    "package_arguments": [],
                }
            ],
        }
        with patch.object(adapter, "_fetch_server_info", return_value=server_info):
            adapter.configure_mcp_server("owner/original-name", server_name="custom-key")
        config = adapter.get_current_config()
        assert "custom-key" in config.get("mcpServers", {})

    def test_returns_false_on_exception(self, tmp_path: Path) -> None:
        """Returns False when an exception is raised during configuration."""
        adapter = self._make_adapter(tmp_path)
        server_info = {
            "id": "uuid-err",
            "name": "broken-server",
            "packages": [],
            "remotes": [],
        }
        with patch.object(adapter, "_fetch_server_info", return_value=server_info):
            result = adapter.configure_mcp_server("owner/broken-server")
        assert result is False


# ===========================================================================
# CopilotClientAdapter — _collect_previously_baked_keys
# ===========================================================================


class TestCollectPreviouslyBakedKeys:
    """Detect previously-baked literal env keys in on-disk config."""

    def _make_adapter(
        self, tmp_path: Path, initial_config: dict | None = None
    ) -> CopilotClientAdapter:
        adapter = CopilotClientAdapter()
        config_path = tmp_path / "mcp-config.json"
        if initial_config is not None:
            config_path.write_text(json.dumps(initial_config))
        adapter.get_config_path = lambda: str(config_path)  # type: ignore[method-assign]
        return adapter

    def test_detects_baked_env_keys(self, tmp_path: Path) -> None:
        """Returns keys that have literal (non-placeholder) values."""
        config = {
            "mcpServers": {
                "my-server": {"env": {"SECRET_KEY": "literal_value", "OTHER": "${OTHER}"}}
            }
        }
        adapter = self._make_adapter(tmp_path, config)
        baked_keys, _ = adapter._collect_previously_baked_keys("owner/my-server", None)
        assert "SECRET_KEY" in baked_keys
        assert "OTHER" not in baked_keys

    def test_detects_baked_headers(self, tmp_path: Path) -> None:
        """Returns headers_were_baked=True when headers contain literals."""
        config = {
            "mcpServers": {"my-server": {"headers": {"Authorization": "Bearer literal-token"}}}
        }
        adapter = self._make_adapter(tmp_path, config)
        _, headers_were_baked = adapter._collect_previously_baked_keys("owner/my-server", None)
        assert headers_were_baked is True

    def test_no_baked_keys_when_no_existing_config(self, tmp_path: Path) -> None:
        """Returns empty set when no existing config file."""
        adapter = self._make_adapter(tmp_path, None)
        baked_keys, headers_were_baked = adapter._collect_previously_baked_keys(
            "owner/server", None
        )
        assert baked_keys == set()
        assert headers_were_baked is False

    def test_server_name_overrides_url_derivation(self, tmp_path: Path) -> None:
        """Uses explicit server_name for config key lookup."""
        config = {"mcpServers": {"custom-key": {"env": {"A_KEY": "baked_value"}}}}
        adapter = self._make_adapter(tmp_path, config)
        baked_keys, _ = adapter._collect_previously_baked_keys("owner/other-name", "custom-key")
        assert "A_KEY" in baked_keys


# ===========================================================================
# CopilotClientAdapter — emit_install_run_summary / reset_install_run_state
# ===========================================================================


class TestEmitInstallRunSummary:
    """Post-install summary emission and state management."""

    def setup_method(self) -> None:
        CopilotClientAdapter.reset_install_run_state()

    def teardown_method(self) -> None:
        CopilotClientAdapter.reset_install_run_state()

    def test_emit_summary_is_idempotent(self, capsys: pytest.CaptureFixture) -> None:
        """Subsequent calls to emit_install_run_summary are no-ops."""
        CopilotClientAdapter._security_upgraded_keys = {"SOME_KEY"}
        CopilotClientAdapter.emit_install_run_summary()
        CopilotClientAdapter.emit_install_run_summary()  # second call is no-op
        assert CopilotClientAdapter._install_run_summary_emitted is True

    def test_reset_clears_all_state(self) -> None:
        """reset_install_run_state clears all class-level state."""
        CopilotClientAdapter._legacy_angle_offenders_by_server = {"s1": {"VAR"}}
        CopilotClientAdapter._security_upgraded_keys = {"KEY"}
        CopilotClientAdapter._unset_env_keys_by_server = {"s1": ["KEY"]}
        CopilotClientAdapter._install_run_summary_emitted = True
        CopilotClientAdapter.reset_install_run_state()
        assert CopilotClientAdapter._legacy_angle_offenders_by_server == {}
        assert CopilotClientAdapter._security_upgraded_keys == set()
        assert CopilotClientAdapter._unset_env_keys_by_server == {}
        assert CopilotClientAdapter._install_run_summary_emitted is False

    def test_unset_env_warning_emitted_when_vars_not_exported(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """Warning emitted when env vars referenced by config are not set."""
        CopilotClientAdapter._unset_env_keys_by_server = {"my-server": ["UNSET_VAR_XYZ_9999"]}
        with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
            CopilotClientAdapter.emit_install_run_summary()
        mock_warn.assert_called()
        call_args = mock_warn.call_args_list
        warning_text = " ".join(str(a) for call in call_args for a in call.args)
        assert "UNSET_VAR_XYZ_9999" in warning_text

    def test_legacy_angle_deprecation_emitted(self, capsys: pytest.CaptureFixture) -> None:
        """Deprecation warning emitted for legacy <VAR> usage."""
        CopilotClientAdapter._legacy_angle_offenders_by_server = {"legacy-server": {"OLD_TOKEN"}}
        with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
            CopilotClientAdapter.emit_install_run_summary()
        mock_warn.assert_called()
        call_args_str = " ".join(str(c) for c in mock_warn.call_args_list)
        assert "legacy-server" in call_args_str

    def test_security_upgrade_warning_emitted(self) -> None:
        """Security upgrade warning emitted when keys were previously baked."""
        CopilotClientAdapter._security_upgraded_keys = {"BAKED_SECRET"}
        with patch("apm_cli.adapters.client.copilot._rich_warning") as mock_warn:
            CopilotClientAdapter.emit_install_run_summary()
        mock_warn.assert_called()
        call_args_str = " ".join(str(c) for c in mock_warn.call_args_list)
        assert "BAKED_SECRET" in call_args_str


# ===========================================================================
# CopilotClientAdapter — _is_github_server
# ===========================================================================


class TestIsGithubServer:
    """Secure detection of GitHub MCP servers to prevent token injection."""

    def _make_adapter(self) -> CopilotClientAdapter:
        return CopilotClientAdapter()

    def test_github_mcp_server_name_and_url(self) -> None:
        """Returns True for known GitHub server name and api.github.com URL."""
        adapter = self._make_adapter()
        assert adapter._is_github_server("github-mcp-server", "https://api.github.com/mcp") is True

    def test_non_github_server_name(self) -> None:
        """Returns False for an unknown server name."""
        adapter = self._make_adapter()
        assert adapter._is_github_server("evil-server", "https://api.github.com/mcp") is False

    def test_non_github_hostname(self) -> None:
        """Returns False for a non-GitHub hostname even with valid server name."""
        adapter = self._make_adapter()
        assert (
            adapter._is_github_server("github-mcp-server", "https://evil.example.com/mcp") is False
        )

    def test_http_url_rejected(self) -> None:
        """Non-HTTPS URL returns False to prevent cleartext token leakage."""
        adapter = self._make_adapter()
        assert adapter._is_github_server("github-mcp-server", "http://api.github.com/mcp") is False

    def test_empty_url_returns_false(self) -> None:
        """Empty URL returns False."""
        adapter = self._make_adapter()
        assert adapter._is_github_server("github-mcp-server", "") is False

    def test_githubcopilot_hostname_accepted(self) -> None:
        """api.githubcopilot.com hostname is accepted."""
        adapter = self._make_adapter()
        assert (
            adapter._is_github_server("github-mcp-server", "https://api.githubcopilot.com/mcp")
            is True
        )
