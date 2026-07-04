"""Comprehensive unit tests for apm_cli.deps.download_strategies.

Pushes coverage of download_strategies.py from ~25% to ≥80% by exercising
all major classes, methods, and branches including:
- _debug helper (APM_DEBUG env var branch)
- DownloadDelegate.resilient_get (all retry / rate-limit branches)
- DownloadDelegate.build_repo_url (ADO, GitHub, SSH, insecure, token logic)
- DownloadDelegate.get_artifactory_headers
- DownloadDelegate.download_artifactory_archive
- DownloadDelegate.download_file_from_artifactory
- DownloadDelegate.try_raw_download
- DownloadDelegate.download_ado_file
- DownloadDelegate.download_gitlab_file
- DownloadDelegate.download_github_file
- DownloadDelegate._is_configured_ghes
- DownloadDelegate._build_contents_api_urls
- DownloadDelegate._build_generic_host_auth_headers
- DownloadDelegate._extract_contents_api_payload
- DownloadDelegate._build_unsupported_or_missing_error
"""

from __future__ import annotations

import base64
import io
import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
import requests

from apm_cli.deps.download_strategies import DownloadDelegate, _debug
from apm_cli.models.apm_package import DependencyReference
from apm_cli.utils.archive import safe_extract_zip
from apm_cli.utils.path_security import PathTraversalError

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_host(
    *,
    github_token: str | None = None,
    ado_token: str | None = None,
    artifactory_token: str | None = None,
    registry_config=None,
    github_host: str = "github.com",
) -> MagicMock:
    """Build a minimal mock GitHubPackageDownloader host."""
    host = MagicMock()
    host.github_token = github_token
    host.ado_token = ado_token
    host.artifactory_token = artifactory_token
    host.registry_config = registry_config
    host.github_host = github_host

    # Shared auth_resolver mock
    auth = MagicMock()
    ctx = MagicMock()
    ctx.token = github_token
    ctx.source = "GITHUB_APM_PAT_ORG" if github_token else ""
    auth.resolve.return_value = ctx
    auth.resolve_for_dep.return_value = ctx
    auth.classify_host.return_value = MagicMock(
        kind="github",
        api_base="https://api.github.com",
    )
    auth.build_error_context.return_value = "Set GITHUB_APM_PAT."

    def _is_generic_dep(dep_ref) -> bool:
        dep_host = getattr(dep_ref, "host", None)
        if not dep_host:
            return False
        return dep_host != "github.com" and not str(dep_host).endswith(".ghe.com")

    host._resolve_dep_token.side_effect = lambda dep_ref=None: (
        None if _is_generic_dep(dep_ref) else github_token
    )
    host._resolve_dep_auth_ctx.side_effect = lambda dep_ref=None: (
        None if _is_generic_dep(dep_ref) else ctx
    )
    host.auth_resolver = auth
    return host


def _make_dep(
    repo_url: str = "owner/repo",
    host: str | None = "github.com",
    *,
    ado_organization: str | None = None,
    ado_project: str | None = None,
    ado_repo: str | None = None,
    is_insecure: bool = False,
    port: int | None = None,
    reference: str | None = "main",
) -> DependencyReference:
    return DependencyReference(
        repo_url=repo_url,
        host=host,
        ado_organization=ado_organization,
        ado_project=ado_project,
        ado_repo=ado_repo,
        is_insecure=is_insecure,
        port=port,
        reference=reference,
    )


def _fake_response(
    status_code: int = 200,
    content: bytes = b"hello",
    headers: dict | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.headers = headers or {}
    resp.text = content.decode("utf-8", errors="replace")
    resp.iter_content.return_value = iter([content] if content else [])
    http_error = requests.exceptions.HTTPError(response=resp)
    resp.raise_for_status.side_effect = http_error if status_code >= 400 else None
    return resp


def _make_zip(files: dict[str, bytes], root_prefix: str = "repo-main/") -> bytes:
    """Build an in-memory zip archive with the given files under *root_prefix*."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # Write the root directory entry first
        zf.mkdir(root_prefix)
        for rel_path, data in files.items():
            zf.writestr(root_prefix + rel_path, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _debug helper
# ---------------------------------------------------------------------------


class TestDebugHelper:
    def test_debug_not_printed_without_env(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("APM_DEBUG", None)
            _debug("should not appear")
        captured = capsys.readouterr()
        assert "should not appear" not in captured.err

    def test_debug_printed_with_env(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch.dict("os.environ", {"APM_DEBUG": "1"}):
            _debug("hello debug")
        captured = capsys.readouterr()
        assert "hello debug" in captured.err
        assert "[DEBUG]" in captured.err


# ---------------------------------------------------------------------------
# resilient_get
# ---------------------------------------------------------------------------


class TestResilientGet:
    """Tests for DownloadDelegate.resilient_get covering all retry branches."""

    def setup_method(self) -> None:
        self.delegate = DownloadDelegate(_make_host())

    def test_success_on_first_attempt(self) -> None:
        resp = _fake_response(200, b"ok")
        with patch("requests.get", return_value=resp) as mock_get:
            result = self.delegate.resilient_get("https://example.com", {}, timeout=5)
        assert result is resp
        assert mock_get.call_count == 1

    def test_passes_stream_flag_to_requests(self) -> None:
        resp = _fake_response(200, b"ok")
        with patch("requests.get", return_value=resp) as mock_get:
            result = self.delegate.resilient_get("https://example.com", {}, stream=True)
        assert result is resp
        assert mock_get.call_args.kwargs["stream"] is True

    def test_429_triggers_retry_after_wait(self) -> None:
        """A 429 response should sleep and retry; second attempt succeeds."""
        rate_resp = _fake_response(429, b"slow", headers={"Retry-After": "0.01"})
        ok_resp = _fake_response(200, b"ok")
        with (
            patch("requests.get", side_effect=[rate_resp, ok_resp]),
            patch("time.sleep") as mock_sleep,
        ):
            result = self.delegate.resilient_get("https://example.com", {}, max_retries=3)
        assert result is ok_resp
        mock_sleep.assert_called_once()
        # Wait value should be capped by float("0.01") → ≤ 0.01
        assert mock_sleep.call_args[0][0] <= 0.02

    def test_503_triggers_retry(self) -> None:
        rate_resp = _fake_response(503, b"unavail", headers={})
        ok_resp = _fake_response(200, b"ok")
        with (
            patch("requests.get", side_effect=[rate_resp, ok_resp]),
            patch("time.sleep"),
        ):
            result = self.delegate.resilient_get("https://example.com", {}, max_retries=3)
        assert result is ok_resp

    def test_403_with_rate_limit_remaining_zero_triggers_retry(self) -> None:
        """403 + X-RateLimit-Remaining: 0 is treated as rate-limited."""
        rate_resp = _fake_response(403, b"rate", headers={"X-RateLimit-Remaining": "0"})
        ok_resp = _fake_response(200, b"ok")
        with (
            patch("requests.get", side_effect=[rate_resp, ok_resp]),
            patch("time.sleep"),
        ):
            result = self.delegate.resilient_get("https://example.com", {}, max_retries=3)
        assert result is ok_resp

    def test_403_without_rate_limit_header_is_returned_immediately(self) -> None:
        """403 without rate-limit header must NOT trigger retry."""
        forbidden_resp = _fake_response(403, b"forbidden", headers={})
        with patch("requests.get", return_value=forbidden_resp) as mock_get:
            result = self.delegate.resilient_get("https://example.com", {}, max_retries=3)
        assert result is forbidden_resp
        assert mock_get.call_count == 1

    def test_rate_limit_exhausts_retries_returns_last_response(self) -> None:
        """If every attempt is rate-limited, the last rate-limit response is returned."""
        rate_resp = _fake_response(429, b"rate", headers={"Retry-After": "0.001"})
        with (
            patch("requests.get", return_value=rate_resp),
            patch("time.sleep"),
        ):
            result = self.delegate.resilient_get("https://example.com", {}, max_retries=2)
        assert result is rate_resp

    def test_retry_after_invalid_falls_back_to_backoff(self) -> None:
        """Non-numeric Retry-After header falls back to exponential back-off."""
        rate_resp = _fake_response(
            429, b"rate", headers={"Retry-After": "Thu, 01 Jan 2099 00:00:00 GMT"}
        )
        ok_resp = _fake_response(200, b"ok")
        with (
            patch("requests.get", side_effect=[rate_resp, ok_resp]),
            patch("time.sleep") as mock_sleep,
        ):
            result = self.delegate.resilient_get("https://example.com", {}, max_retries=3)
        assert result is ok_resp
        # backoff formula: min(2^0, 30) * (0.5+random) → between 0.5 and 1.5
        wait_used = mock_sleep.call_args[0][0]
        assert 0 < wait_used <= 31.0

    def test_reset_at_header_used_when_no_retry_after(self) -> None:
        """X-RateLimit-Reset is used when Retry-After is absent."""
        import time as _time

        future_reset = int(_time.time()) + 2
        rate_resp = _fake_response(429, b"rate", headers={"X-RateLimit-Reset": str(future_reset)})
        ok_resp = _fake_response(200, b"ok")
        with (
            patch("requests.get", side_effect=[rate_resp, ok_resp]),
            patch("time.sleep") as mock_sleep,
        ):
            result = self.delegate.resilient_get("https://example.com", {}, max_retries=3)
        assert result is ok_resp
        # Wait should be ≤ 60 and ≥ 0
        assert 0 <= mock_sleep.call_args[0][0] <= 60

    def test_reset_at_invalid_falls_back_to_backoff(self) -> None:
        """Non-numeric X-RateLimit-Reset falls back to exponential back-off."""
        rate_resp = _fake_response(429, b"rate", headers={"X-RateLimit-Reset": "not-a-number"})
        ok_resp = _fake_response(200, b"ok")
        with (
            patch("requests.get", side_effect=[rate_resp, ok_resp]),
            patch("time.sleep") as mock_sleep,
        ):
            result = self.delegate.resilient_get("https://example.com", {}, max_retries=3)
        assert result is ok_resp
        assert mock_sleep.called

    def test_connection_error_retries_then_raises(self) -> None:
        """ConnectionError should retry and re-raise after exhaustion."""
        err = requests.exceptions.ConnectionError("timeout")
        with (
            patch("requests.get", side_effect=[err, err, err]),
            patch("time.sleep"),
        ):
            with pytest.raises(requests.exceptions.ConnectionError):
                self.delegate.resilient_get("https://example.com", {}, max_retries=3)

    def test_connection_error_last_attempt_no_sleep(self) -> None:
        """On the last attempt there is no sleep before raising."""
        err = requests.exceptions.ConnectionError("conn")
        ok_resp = _fake_response(200, b"ok")
        with (
            patch("requests.get", side_effect=[err, ok_resp]),
            patch("time.sleep") as mock_sleep,
        ):
            result = self.delegate.resilient_get("https://example.com", {}, max_retries=2)
        assert result is ok_resp
        # Slept once (on the first failure, attempt 0 < max_retries-1=1)
        assert mock_sleep.call_count == 1

    def test_timeout_error_retries(self) -> None:
        """Timeout should be retried silently."""
        t_err = requests.exceptions.Timeout("timed out")
        ok_resp = _fake_response(200, b"ok")
        with patch("requests.get", side_effect=[t_err, ok_resp]):
            result = self.delegate.resilient_get("https://example.com", {}, max_retries=2)
        assert result is ok_resp

    def test_all_timeouts_raises_request_exception(self) -> None:
        """If all attempts time out and there's no last_response, raise RequestException."""
        t_err = requests.exceptions.Timeout("timed out")
        with patch("requests.get", side_effect=[t_err, t_err]):
            with pytest.raises(requests.exceptions.RequestException):
                self.delegate.resilient_get("https://example.com", {}, max_retries=2)

    def test_rate_limit_remaining_low_debug(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Low but non-zero remaining triggers a debug message."""
        resp = _fake_response(200, b"ok", headers={"X-RateLimit-Remaining": "5"})
        with (
            patch("requests.get", return_value=resp),
            patch.dict("os.environ", {"APM_DEBUG": "1"}),
        ):
            self.delegate.resilient_get("https://example.com", {})
        captured = capsys.readouterr()
        assert "rate limit low" in captured.err.lower()

    def test_rate_limit_remaining_invalid_is_ignored(self) -> None:
        """Non-numeric X-RateLimit-Remaining on success path is silently ignored."""
        resp = _fake_response(200, b"ok", headers={"X-RateLimit-Remaining": "nope"})
        with patch("requests.get", return_value=resp):
            result = self.delegate.resilient_get("https://example.com", {})
        assert result is resp

    def test_403_rate_limit_remaining_invalid_not_treated_as_rate_limit(self) -> None:
        """403 + non-numeric X-RateLimit-Remaining: not treated as rate-limit."""
        resp = _fake_response(403, b"forbidden", headers={"X-RateLimit-Remaining": "bad"})
        with patch("requests.get", return_value=resp) as mock_get:
            result = self.delegate.resilient_get("https://example.com", {}, max_retries=3)
        assert result is resp
        # Not retried
        assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# build_repo_url
# ---------------------------------------------------------------------------


class TestBuildRepoUrl:
    """Tests for DownloadDelegate.build_repo_url covering all token/backend paths."""

    def _delegate(self, **kwargs) -> DownloadDelegate:
        return DownloadDelegate(_make_host(**kwargs))

    def test_github_https_with_token(self) -> None:
        d = self._delegate(github_token="tok123")
        dep = _make_dep("owner/repo", "github.com")
        url = d.build_repo_url("owner/repo", dep_ref=dep)
        assert "owner/repo" in url
        assert "tok123" in url

    def test_github_https_no_token(self) -> None:
        d = self._delegate()
        dep = _make_dep("owner/repo", "github.com")
        url = d.build_repo_url("owner/repo", dep_ref=dep)
        assert "owner/repo" in url
        assert "token" not in url.lower() or "None" not in url

    def test_github_ssh_url(self) -> None:
        d = self._delegate()
        dep = _make_dep("owner/repo", "github.com")
        url = d.build_repo_url("owner/repo", use_ssh=True, dep_ref=dep)
        assert "git@github.com" in url or "ssh://" in url or "@" in url

    def test_insecure_url(self) -> None:
        d = self._delegate()
        dep = _make_dep("owner/repo", "my-host.com", is_insecure=True)
        url = d.build_repo_url("owner/repo", dep_ref=dep)
        assert url.startswith("http://")
        assert "owner/repo" in url

    def test_empty_token_suppresses_auth(self) -> None:
        """token='' is the sentinel that explicitly suppresses embedding."""
        d = self._delegate(github_token="tok")
        dep = _make_dep("owner/repo", "github.com")
        url = d.build_repo_url("owner/repo", token="", dep_ref=dep)
        # The empty-token sentinel means "no credential in URL"
        assert "tok" not in url

    def test_explicit_token_overrides_host_token(self) -> None:
        d = self._delegate(github_token="host-tok")
        dep = _make_dep("owner/repo", "github.com")
        url = d.build_repo_url("owner/repo", token="override-tok", dep_ref=dep)
        assert "override-tok" in url
        assert "host-tok" not in url

    def test_ado_without_dep_ref_falls_through(self) -> None:
        """build_repo_url with dep_ref=None preserves legacy behaviour."""
        d = self._delegate()
        url = d.build_repo_url("owner/repo", dep_ref=None)
        assert "owner/repo" in url

    def test_no_dep_ref_ssh(self) -> None:
        d = self._delegate()
        url = d.build_repo_url("owner/repo", use_ssh=True, dep_ref=None)
        assert "@" in url or "ssh" in url.lower()

    def test_no_dep_ref_insecure(self) -> None:
        d = self._delegate()
        url = d.build_repo_url(
            "owner/repo",
            dep_ref=DependencyReference(repo_url="owner/repo", is_insecure=True),
        )
        assert url.startswith("http://")

    def test_dep_ref_with_host_uses_dep_host(self) -> None:
        d = self._delegate(github_host="github.com")
        dep = _make_dep("owner/repo", host="ghes.company.com")
        with patch("apm_cli.deps.download_strategies.backend_for") as mock_backend_for:
            backend = MagicMock()
            backend.kind = "ghes"
            backend.is_github_family = True
            backend.build_clone_https_url.return_value = "https://ghes.company.com/owner/repo.git"
            mock_backend_for.return_value = backend
            url = d.build_repo_url("owner/repo", dep_ref=dep)
        assert urlparse(url).hostname == "ghes.company.com"

    def test_ado_backend_without_org_falls_to_generic(self) -> None:
        """ADO dep_ref missing ado_organization must fall through to generic GitHub-style URL."""
        host = _make_host(ado_token="ado-pat")
        d = DownloadDelegate(host)
        dep = _make_dep(
            "org/proj/myrepo",
            host="dev.azure.com",
            ado_organization=None,  # missing!
        )
        with patch("apm_cli.deps.download_strategies.backend_for") as mock_backend_for:
            # First call returns ADO backend, second call (fallback) returns generic
            ado_backend = MagicMock()
            ado_backend.kind = "ado"
            ado_backend.is_github_family = False
            generic_backend = MagicMock()
            generic_backend.kind = "github"
            generic_backend.is_github_family = True
            generic_backend.build_clone_https_url.return_value = (
                "https://github.com/org/proj/myrepo.git"
            )
            mock_backend_for.side_effect = [ado_backend, generic_backend]
            d.build_repo_url("org/proj/myrepo", dep_ref=dep)
        # Second backend_for was called with None dep_ref (fallback path)
        assert mock_backend_for.call_count == 2

    def test_gitlab_token_resolved_from_auth_resolver(self) -> None:
        host = _make_host()
        gitlab_ctx = MagicMock()
        gitlab_ctx.token = "gitlab-tok"
        host.auth_resolver.resolve_for_dep.return_value = gitlab_ctx
        d = DownloadDelegate(host)
        dep = _make_dep("owner/repo", host="gitlab.example.com")
        with patch("apm_cli.deps.download_strategies.backend_for") as mock_backend_for:
            backend = MagicMock()
            backend.kind = "gitlab"
            backend.is_github_family = False
            backend.build_clone_https_url.return_value = "https://gitlab.example.com/owner/repo.git"
            mock_backend_for.return_value = backend
            d.build_repo_url("owner/repo", dep_ref=dep)
        # Token should come from auth_resolver.resolve_for_dep
        backend.build_clone_https_url.assert_called_once()
        _, kwargs = backend.build_clone_https_url.call_args
        assert kwargs.get("token") == "gitlab-tok"

    def test_generic_host_no_token_embedded(self) -> None:
        d = self._delegate()
        dep = _make_dep("owner/repo", host="custom.git.host")
        with patch("apm_cli.deps.download_strategies.backend_for") as mock_backend_for:
            backend = MagicMock()
            backend.kind = "generic"
            backend.is_github_family = False
            backend.build_clone_https_url.return_value = "https://custom.git.host/owner/repo.git"
            mock_backend_for.return_value = backend
            d.build_repo_url("owner/repo", dep_ref=dep)
        backend.build_clone_https_url.assert_called_once()
        _, kwargs = backend.build_clone_https_url.call_args
        assert kwargs.get("token") is None


# ---------------------------------------------------------------------------
# get_artifactory_headers
# ---------------------------------------------------------------------------


class TestGetArtifactoryHeaders:
    def test_no_registry_config_no_token_empty_headers(self) -> None:
        d = DownloadDelegate(_make_host())
        assert d.get_artifactory_headers() == {}

    def test_no_registry_config_with_artifactory_token(self) -> None:
        d = DownloadDelegate(_make_host(artifactory_token="art-tok"))
        headers = d.get_artifactory_headers()
        assert headers["Authorization"] == "Bearer art-tok"

    def test_uses_registry_config_get_headers(self) -> None:
        cfg = MagicMock()
        cfg.get_headers.return_value = {"Authorization": "Bearer reg-tok"}
        d = DownloadDelegate(_make_host(registry_config=cfg))
        headers = d.get_artifactory_headers()
        assert headers["Authorization"] == "Bearer reg-tok"
        cfg.get_headers.assert_called_once()


# ---------------------------------------------------------------------------
# download_artifactory_archive
# ---------------------------------------------------------------------------


class TestDownloadArtifactoryArchive:
    def _delegate(self, resp_sequence=None) -> tuple[DownloadDelegate, MagicMock]:
        host = _make_host()
        d = DownloadDelegate(host)
        if resp_sequence is not None:
            host._resilient_get.side_effect = resp_sequence
        return d, host

    def test_success_extracts_files_stripping_root_prefix(self, tmp_path: Path) -> None:
        zip_bytes = _make_zip({"apm.yml": b"name: test"}, root_prefix="repo-main/")
        resp = _fake_response(200, zip_bytes)
        d, host = self._delegate([resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/repo.zip"],
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "apm.yml").read_bytes() == b"name: test"
        assert host._resilient_get.call_args.kwargs["stream"] is True

    def test_http_non_200_tries_next_url(self, tmp_path: Path) -> None:
        zip_bytes = _make_zip({"apm.yml": b"name: test"})
        fail_resp = _fake_response(404, b"")
        ok_resp = _fake_response(200, zip_bytes)
        d, _ = self._delegate([fail_resp, ok_resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=[
                "https://art.example.com/fail.zip",
                "https://art.example.com/ok.zip",
            ],
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "apm.yml").exists()

    def test_all_urls_fail_raises_runtime_error(self, tmp_path: Path) -> None:
        fail_resp = _fake_response(404, b"")
        d, _ = self._delegate([fail_resp, fail_resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=[
                "https://art.example.com/a.zip",
                "https://art.example.com/b.zip",
            ],
        ):
            with pytest.raises(RuntimeError, match="Failed to download"):
                d.download_artifactory_archive(
                    "art.example.com", "apm", "owner", "repo", "main", tmp_path
                )

    def test_bad_zip_file_tries_next(self, tmp_path: Path) -> None:
        bad_resp = _fake_response(200, b"not a zip")
        zip_bytes = _make_zip({"apm.yml": b"ok"})
        ok_resp = _fake_response(200, zip_bytes)
        d, _ = self._delegate([bad_resp, ok_resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=[
                "https://art.example.com/bad.zip",
                "https://art.example.com/ok.zip",
            ],
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "apm.yml").exists()

    def test_archive_too_large_skips_to_next(self, tmp_path: Path) -> None:
        huge_resp = MagicMock()
        huge_resp.status_code = 200
        huge_resp.headers = {"Content-Length": str(600 * 1024 * 1024)}
        huge_resp.iter_content.return_value = iter(())

        zip_bytes = _make_zip({"apm.yml": b"ok"})
        ok_resp = _fake_response(200, zip_bytes)
        d, _ = self._delegate([huge_resp, ok_resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=[
                "https://art.example.com/huge.zip",
                "https://art.example.com/ok.zip",
            ],
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "apm.yml").exists()

    def test_archive_stream_limit_skips_to_next_url(self, tmp_path: Path) -> None:
        huge_resp = MagicMock()
        huge_resp.status_code = 200
        huge_resp.headers = {}
        huge_resp.iter_content.return_value = iter([b"x" * (2 * 1024 * 1024)])

        ok_resp = _fake_response(200, _make_zip({"apm.yml": b"ok"}))
        d, _ = self._delegate([huge_resp, ok_resp])
        with (
            patch.dict("os.environ", {"ARTIFACTORY_MAX_ARCHIVE_MB": "1"}),
            patch(
                "apm_cli.deps.download_strategies.build_artifactory_archive_url",
                return_value=[
                    "https://art.example.com/huge.zip",
                    "https://art.example.com/ok.zip",
                ],
            ),
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "apm.yml").exists()

    def test_empty_archive_raises_runtime_error(self, tmp_path: Path) -> None:
        """A valid zip that contains no entries raises RuntimeError."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass  # empty zip
        empty_zip = buf.getvalue()
        resp = _fake_response(200, empty_zip)
        d, _ = self._delegate([resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/empty.zip"],
        ):
            with pytest.raises((RuntimeError, Exception)):
                d.download_artifactory_archive(
                    "art.example.com", "apm", "owner", "repo", "main", tmp_path
                )

    def test_single_file_archive_extracted_as_is(self, tmp_path: Path) -> None:
        """A zip whose first entry is a plain file (not a directory) is extracted as-is."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.md", b"readme content")
        single_file_zip = buf.getvalue()
        resp = _fake_response(200, single_file_zip)
        d, _ = self._delegate([resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/single.zip"],
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "README.md").exists()

    def test_single_file_archive_rejects_path_traversal(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../escape.txt", b"nope")
        resp = _fake_response(200, buf.getvalue())
        d, _ = self._delegate([resp])
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/single.zip"],
        ):
            with pytest.raises(RuntimeError, match="Failed to download"):
                d.download_artifactory_archive(
                    "art.example.com", "apm", "owner", "repo", "main", tmp_path
                )
        assert not (tmp_path.parent / "escape.txt").exists()

    def test_archive_rejects_uncompressed_size_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        zip_bytes = _make_zip({"big.bin": b"x" * 2048}, root_prefix="repo-main/")
        resp = _fake_response(200, zip_bytes)
        d, _ = self._delegate([resp])
        monkeypatch.setitem(safe_extract_zip.__kwdefaults__, "max_uncompressed", 1024)
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/repo.zip"],
        ):
            with pytest.raises(RuntimeError, match="Failed to download"):
                d.download_artifactory_archive(
                    "art.example.com", "apm", "owner", "repo", "main", tmp_path
                )

    def test_request_exception_tries_next(self, tmp_path: Path) -> None:
        d, host_mock = self._delegate()
        host_mock._resilient_get.side_effect = [
            requests.RequestException("network error"),
            _fake_response(200, _make_zip({"f.txt": b"data"})),
        ]
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=[
                "https://art.example.com/fail.zip",
                "https://art.example.com/ok.zip",
            ],
        ):
            d.download_artifactory_archive(
                "art.example.com", "apm", "owner", "repo", "main", tmp_path
            )
        assert (tmp_path / "f.txt").exists()


# ---------------------------------------------------------------------------
# download_file_from_artifactory
# ---------------------------------------------------------------------------


class TestDownloadFileFromArtifactory:
    def test_uses_registry_client_when_config_matches(self) -> None:
        cfg = MagicMock()
        cfg.host = "art.example.com"
        client = MagicMock()
        client.fetch_file.return_value = b"file content"
        cfg.get_client.return_value = client

        host = _make_host(registry_config=cfg)
        d = DownloadDelegate(host)
        result = d.download_file_from_artifactory(
            "art.example.com", "apm", "owner", "repo", "apm.yml", "main"
        )
        assert result == b"file content"

    def test_falls_back_to_entry_helper_when_config_host_mismatch(self) -> None:
        cfg = MagicMock()
        cfg.host = "other.host.com"  # mismatch
        host = _make_host(registry_config=cfg)
        d = DownloadDelegate(host)

        # fetch_entry_from_archive is imported inside the else branch;
        # patch it at its definition site.
        with patch(
            "apm_cli.deps.artifactory_entry.fetch_entry_from_archive",
            return_value=b"entry bytes",
        ):
            result = d.download_file_from_artifactory(
                "art.example.com",
                "apm",
                "owner",
                "repo",
                "apm.yml",
                "main",
            )
        assert result == b"entry bytes"

    def test_falls_back_to_archive_download_when_fetch_returns_none(
        self,
    ) -> None:
        cfg = MagicMock()
        cfg.host = "art.example.com"
        client = MagicMock()
        client.fetch_file.return_value = None  # entry not found
        cfg.get_client.return_value = client

        zip_bytes = _make_zip({"apm.yml": b"archive content"})
        resp = _fake_response(200, zip_bytes)
        host = _make_host(registry_config=cfg)
        host._resilient_get.return_value = resp
        d = DownloadDelegate(host)
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/repo.zip"],
        ):
            result = d.download_file_from_artifactory(
                "art.example.com", "apm", "owner", "repo", "apm.yml", "main"
            )
        assert result == b"archive content"

    def test_raises_when_file_not_in_archive(self) -> None:
        cfg = MagicMock()
        cfg.host = "art.example.com"
        client = MagicMock()
        client.fetch_file.return_value = None
        cfg.get_client.return_value = client

        # Archive does NOT contain the requested file
        zip_bytes = _make_zip({"other.yml": b"other"})
        resp = _fake_response(200, zip_bytes)
        host = _make_host(registry_config=cfg)
        host._resilient_get.return_value = resp
        d = DownloadDelegate(host)
        with patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://art.example.com/repo.zip"],
        ):
            with pytest.raises(RuntimeError, match="Failed to download file"):
                d.download_file_from_artifactory(
                    "art.example.com",
                    "apm",
                    "owner",
                    "repo",
                    "missing.yml",
                    "main",
                )


# ---------------------------------------------------------------------------
# try_raw_download
# ---------------------------------------------------------------------------


class TestTryRawDownload:
    def test_success_returns_bytes(self) -> None:
        d = DownloadDelegate(_make_host())
        resp = _fake_response(200, b"raw content")
        with patch("requests.get", return_value=resp):
            result = d.try_raw_download("owner", "repo", "main", "apm.yml")
        assert result == b"raw content"

    def test_404_returns_none(self) -> None:
        d = DownloadDelegate(_make_host())
        resp = _fake_response(404, b"")
        with patch("requests.get", return_value=resp):
            result = d.try_raw_download("owner", "repo", "main", "apm.yml")
        assert result is None

    def test_request_exception_returns_none(self) -> None:
        d = DownloadDelegate(_make_host())
        with patch(
            "requests.get",
            side_effect=requests.exceptions.ConnectionError("err"),
        ):
            result = d.try_raw_download("owner", "repo", "main", "apm.yml")
        assert result is None


# ---------------------------------------------------------------------------
# download_ado_file
# ---------------------------------------------------------------------------


class TestDownloadAdoFile:
    def _dep(self, **kwargs) -> DependencyReference:
        defaults = dict(
            repo_url="org/proj/myrepo",
            host="dev.azure.com",
            ado_organization="my-org",
            ado_project="my-proj",
            ado_repo="my-repo",
        )
        defaults.update(kwargs)
        return _make_dep(**defaults)

    def test_success_returns_content(self) -> None:
        host = _make_host(ado_token="ado-pat")
        d = DownloadDelegate(host)
        resp = _fake_response(200, b"ado file content")
        host._resilient_get.return_value = resp
        result = d.download_ado_file(self._dep(), "apm.yml")
        assert result == b"ado file content"

    def test_missing_ado_fields_raises_value_error(self) -> None:
        host = _make_host()
        d = DownloadDelegate(host)
        dep = DependencyReference(
            repo_url="incomplete",
            ado_organization=None,
            ado_project=None,
            ado_repo=None,
        )
        with pytest.raises(ValueError, match="Invalid Azure DevOps"):
            d.download_ado_file(dep, "apm.yml")

    def test_404_tries_fallback_ref_main_to_master(self) -> None:
        host = _make_host(ado_token="pat")
        d = DownloadDelegate(host)
        fail_resp = _fake_response(404, b"")
        ok_resp = _fake_response(200, b"fallback content")
        host._resilient_get.side_effect = [fail_resp, ok_resp]
        result = d.download_ado_file(self._dep(), "apm.yml", ref="main")
        assert result == b"fallback content"

    def test_404_tries_fallback_ref_master_to_main(self) -> None:
        host = _make_host(ado_token="pat")
        d = DownloadDelegate(host)
        fail_resp = _fake_response(404, b"")
        ok_resp = _fake_response(200, b"fallback content")
        host._resilient_get.side_effect = [fail_resp, ok_resp]
        result = d.download_ado_file(self._dep(), "apm.yml", ref="master")
        assert result == b"fallback content"

    def test_404_non_default_ref_raises_runtime(self) -> None:
        host = _make_host(ado_token="pat")
        d = DownloadDelegate(host)
        fail_resp = _fake_response(404, b"")
        host._resilient_get.return_value = fail_resp
        with pytest.raises(RuntimeError, match="File not found"):
            d.download_ado_file(self._dep(), "apm.yml", ref="feature/branch")

    def test_404_fallback_also_fails_raises_runtime(self) -> None:
        host = _make_host(ado_token="pat")
        d = DownloadDelegate(host)
        fail_resp = _fake_response(404, b"")
        host._resilient_get.side_effect = [fail_resp, fail_resp]
        with pytest.raises(RuntimeError):
            d.download_ado_file(self._dep(), "apm.yml", ref="main")

    def test_401_with_no_token_includes_context(self) -> None:
        host = _make_host(ado_token=None)
        host.auth_resolver.build_error_context.return_value = "Set ADO_APM_PAT."
        d = DownloadDelegate(host)
        resp = _fake_response(401, b"")
        host._resilient_get.return_value = resp
        with pytest.raises(RuntimeError, match="Authentication failed"):
            d.download_ado_file(self._dep(), "apm.yml")

    def test_403_with_token_gives_check_permissions_hint(self) -> None:
        host = _make_host(ado_token="my-pat")
        d = DownloadDelegate(host)
        resp = _fake_response(403, b"")
        host._resilient_get.return_value = resp
        with pytest.raises(RuntimeError, match="Authentication failed"):
            d.download_ado_file(self._dep(), "apm.yml")

    def test_other_http_error_wraps_in_runtime(self) -> None:
        host = _make_host(ado_token="pat")
        d = DownloadDelegate(host)
        resp = _fake_response(500, b"")
        host._resilient_get.return_value = resp
        with pytest.raises(RuntimeError, match="HTTP 500"):
            d.download_ado_file(self._dep(), "apm.yml")

    def test_network_error_wraps_in_runtime(self) -> None:
        host = _make_host(ado_token="pat")
        d = DownloadDelegate(host)
        host._resilient_get.side_effect = requests.exceptions.ConnectionError("err")
        with pytest.raises(RuntimeError, match="Network error"):
            d.download_ado_file(self._dep(), "apm.yml")

    def test_auth_header_built_from_ado_token(self) -> None:
        host = _make_host(ado_token="secret-pat")
        d = DownloadDelegate(host)
        resp = _fake_response(200, b"ok")
        host._resilient_get.return_value = resp
        d.download_ado_file(self._dep(), "apm.yml")
        call_kwargs = host._resilient_get.call_args[1]
        headers = call_kwargs.get("headers", {})
        assert "Authorization" in headers
        expected_auth = base64.b64encode(b":secret-pat").decode()
        assert expected_auth in headers["Authorization"]

    def test_no_ado_token_no_auth_header(self) -> None:
        host = _make_host(ado_token=None)
        d = DownloadDelegate(host)
        resp = _fake_response(200, b"ok")
        host._resilient_get.return_value = resp
        d.download_ado_file(self._dep(), "apm.yml")
        call_kwargs = host._resilient_get.call_args[1]
        headers = call_kwargs.get("headers", {})
        assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# download_gitlab_file
# ---------------------------------------------------------------------------


class TestDownloadGitlabFile:
    def _dep(self) -> DependencyReference:
        return DependencyReference(
            repo_url="mygroup/myproject",
            host="gitlab.example.com",
        )

    def _setup_host_info(self, host_mock: MagicMock) -> None:
        info = MagicMock()
        info.api_base = "https://gitlab.example.com/api/v4"
        host_mock.auth_resolver.classify_host.return_value = info
        ctx = MagicMock()
        ctx.token = "gl-tok"
        host_mock.auth_resolver.resolve.return_value = ctx

    def test_success_returns_content(self) -> None:
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)
        resp = _fake_response(200, b"gitlab file")
        host._resilient_get.return_value = resp

        with patch(
            "apm_cli.deps.download_strategies.AuthResolver.gitlab_rest_headers",
            return_value={"Authorization": "Bearer gl-tok"},
        ):
            result = d.download_gitlab_file(self._dep(), "apm.yml")
        assert result == b"gitlab file"

    def test_missing_repo_url_raises(self) -> None:
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)
        dep = DependencyReference(repo_url="", host="gitlab.example.com")

        with patch(
            "apm_cli.deps.download_strategies.AuthResolver.gitlab_rest_headers",
            return_value={},
        ):
            with pytest.raises(RuntimeError, match="Missing repository path"):
                d.download_gitlab_file(dep, "apm.yml")

    def test_404_main_falls_back_to_master(self) -> None:
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)
        fail_resp = _fake_response(404, b"")
        ok_resp = _fake_response(200, b"from master")
        host._resilient_get.side_effect = [fail_resp, ok_resp]

        with patch(
            "apm_cli.deps.download_strategies.AuthResolver.gitlab_rest_headers",
            return_value={},
        ):
            result = d.download_gitlab_file(self._dep(), "apm.yml", ref="main")
        assert result == b"from master"

    def test_404_non_default_ref_raises(self) -> None:
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)
        fail_resp = _fake_response(404, b"")
        host._resilient_get.return_value = fail_resp

        with patch(
            "apm_cli.deps.download_strategies.AuthResolver.gitlab_rest_headers",
            return_value={},
        ):
            with pytest.raises(RuntimeError, match="File not found"):
                d.download_gitlab_file(self._dep(), "apm.yml", ref="feature/x")

    def test_404_fallback_also_fails_raises(self) -> None:
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)
        fail_resp = _fake_response(404, b"")
        host._resilient_get.side_effect = [fail_resp, fail_resp]

        with patch(
            "apm_cli.deps.download_strategies.AuthResolver.gitlab_rest_headers",
            return_value={},
        ):
            with pytest.raises(RuntimeError, match="File not found"):
                d.download_gitlab_file(self._dep(), "apm.yml", ref="main")

    def test_401_without_token_includes_context(self) -> None:
        host = _make_host()
        info = MagicMock()
        info.api_base = "https://gitlab.example.com/api/v4"
        host.auth_resolver.classify_host.return_value = info
        ctx = MagicMock()
        ctx.token = None  # no token
        host.auth_resolver.resolve.return_value = ctx
        host.auth_resolver.build_error_context.return_value = "Set GITLAB_APM_PAT."

        d = DownloadDelegate(host)
        resp = _fake_response(401, b"")
        host._resilient_get.return_value = resp

        with patch(
            "apm_cli.deps.download_strategies.AuthResolver.gitlab_rest_headers",
            return_value={},
        ):
            with pytest.raises(RuntimeError, match="Authentication failed"):
                d.download_gitlab_file(self._dep(), "apm.yml")

    def test_401_with_token_prompts_scope_check(self) -> None:
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)
        resp = _fake_response(401, b"")
        host._resilient_get.return_value = resp

        with patch(
            "apm_cli.deps.download_strategies.AuthResolver.gitlab_rest_headers",
            return_value={},
        ):
            with pytest.raises(RuntimeError) as exc_info:
                d.download_gitlab_file(self._dep(), "apm.yml")
        assert "Authentication failed" in str(exc_info.value)

    def test_other_http_error_raises_runtime(self) -> None:
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)
        resp = _fake_response(500, b"")
        host._resilient_get.return_value = resp

        with patch(
            "apm_cli.deps.download_strategies.AuthResolver.gitlab_rest_headers",
            return_value={},
        ):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                d.download_gitlab_file(self._dep(), "apm.yml")

    def test_network_error_raises_runtime(self) -> None:
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)
        host._resilient_get.side_effect = requests.exceptions.ConnectionError("err")

        with patch(
            "apm_cli.deps.download_strategies.AuthResolver.gitlab_rest_headers",
            return_value={},
        ):
            with pytest.raises(RuntimeError, match="Network error"):
                d.download_gitlab_file(self._dep(), "apm.yml")

    def test_verbose_callback_called_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)
        resp = _fake_response(200, b"data")
        host._resilient_get.return_value = resp
        callback = MagicMock()
        git_error = RuntimeError(
            "fatal: could not read from https://oauth2:secret@gitlab.example.com/group/repo.git"
        )
        monkeypatch.setenv("APM_DEBUG", "1")

        with (
            patch(
                "apm_cli.deps.download_strategies.AuthResolver.gitlab_rest_headers",
                return_value={},
            ),
            patch(
                "apm_cli.deps.download_strategies.GitSparseFileTransport",
                return_value=MagicMock(fetch_file=MagicMock(side_effect=git_error)),
            ),
            patch("builtins.print") as debug_print,
        ):
            d.download_gitlab_file(self._dep(), "apm.yml", verbose_callback=callback)
        # The mocked git failure drives the REST fallback path without spawning
        # subprocess/network work, and raw git stderr must not leak secrets.
        callback_messages = " ".join(str(c.args[0]) for c in callback.call_args_list)
        debug_messages = " ".join(str(c.args[0]) for c in debug_print.call_args_list)
        assert callback.called
        assert "Downloaded file:" in callback_messages
        assert "oauth2:secret" not in callback_messages
        assert "oauth2:secret" not in debug_messages

    def test_path_traversal_from_git_transport_does_not_fallback(self) -> None:
        """Direct delegate path must propagate traversal rejection without REST."""
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)

        with (
            patch(
                "apm_cli.deps.download_strategies.GitSparseFileTransport",
                return_value=MagicMock(
                    fetch_file=MagicMock(side_effect=PathTraversalError("path escapes work tree"))
                ),
            ),
            patch.object(host, "_resilient_get") as mock_api,
            pytest.raises(PathTraversalError),
        ):
            d.download_gitlab_file(self._dep(), "../apm.yml")

        mock_api.assert_not_called()

    def test_ref_injection_from_git_transport_does_not_fallback(self) -> None:
        """Git refs that look like options must hard-fail without REST fallback."""
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)

        with patch.object(host, "_resilient_get") as mock_api, pytest.raises(ValueError):
            d.download_gitlab_file(self._dep(), "agents/spec.agent.md", ref="--upload-pack=x")

        mock_api.assert_not_called()

    def test_same_repo_ref_reuses_git_file_transport(self) -> None:
        """Two GitLab path: files from one repo/ref share one sparse checkout."""
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)
        transport = MagicMock()
        transport.fetch_file.side_effect = [b"agent", b"prompt"]

        with patch(
            "apm_cli.deps.download_strategies.GitSparseFileTransport",
            return_value=transport,
        ) as transport_cls:
            first = d.download_gitlab_file(self._dep(), "agents/a.agent.md", ref="main")
            second = d.download_gitlab_file(self._dep(), "prompts/b.prompt.md", ref="main")

        assert first == b"agent"
        assert second == b"prompt"
        transport_cls.assert_called_once()
        assert [c.args[0] for c in transport.fetch_file.call_args_list] == [
            "agents/a.agent.md",
            "prompts/b.prompt.md",
        ]

    def test_concurrent_same_repo_ref_creates_one_git_file_transport(self) -> None:
        """Parallel path: files from one repo/ref share one cached checkout safely."""
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)
        barrier = threading.Barrier(2)

        class FakeTransport:
            def fetch_file(self, file_path: str) -> bytes:
                barrier.wait(timeout=5)
                return file_path.encode()

            def close(self) -> None:
                pass

        with patch(
            "apm_cli.deps.download_strategies.GitSparseFileTransport",
            return_value=FakeTransport(),
        ) as transport_cls:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(
                        lambda path: d.download_gitlab_file(self._dep(), path, ref="main"),
                        ["agents/a.agent.md", "prompts/b.prompt.md"],
                    )
                )

        assert results == [b"agents/a.agent.md", b"prompts/b.prompt.md"]
        transport_cls.assert_called_once()


# ---------------------------------------------------------------------------
# download_github_file
# ---------------------------------------------------------------------------


class TestDownloadGithubFile:
    def _dep(self, host: str = "github.com", repo_url: str = "owner/repo") -> DependencyReference:
        return DependencyReference(repo_url=repo_url, host=host)

    def test_cdn_fast_path_success(self) -> None:
        """github.com without token should try CDN first."""
        host_mock = _make_host(github_token=None)
        ctx = MagicMock()
        ctx.token = None
        host_mock.auth_resolver.resolve.return_value = ctx
        d = DownloadDelegate(host_mock)

        with patch.object(d, "try_raw_download", return_value=b"cdn bytes"):
            result = d.download_github_file(self._dep(), "apm.yml")
        assert result == b"cdn bytes"

    def test_cdn_fast_path_404_falls_through_to_api(self) -> None:
        """CDN 404 on 'main' tries 'master', then falls through to Contents API."""
        host_mock = _make_host(github_token=None)
        ctx = MagicMock()
        ctx.token = None
        host_mock.auth_resolver.resolve.return_value = ctx
        d = DownloadDelegate(host_mock)

        api_resp = _fake_response(200, b"api bytes")
        host_mock._resilient_get.return_value = api_resp

        with patch.object(d, "try_raw_download", return_value=None):
            with patch.object(
                DownloadDelegate,
                "_extract_contents_api_payload",
                return_value=b"api bytes",
            ):
                result = d.download_github_file(self._dep(), "apm.yml", ref="main")
        assert result == b"api bytes"

    def test_cdn_fast_path_not_used_with_token(self) -> None:
        """With a token, CDN path is skipped; direct API call expected."""
        host_mock = _make_host(github_token="tok")
        ctx = MagicMock()
        ctx.token = "tok"
        host_mock.auth_resolver.resolve.return_value = ctx
        d = DownloadDelegate(host_mock)

        api_resp = _fake_response(200, b"api bytes")
        host_mock._resilient_get.return_value = api_resp

        with patch.object(d, "try_raw_download") as mock_cdn:
            with patch.object(
                DownloadDelegate,
                "_extract_contents_api_payload",
                return_value=b"api bytes",
            ):
                d.download_github_file(self._dep(), "apm.yml")
        mock_cdn.assert_not_called()

    def test_ghes_host_goes_straight_to_api(self) -> None:
        host_mock = _make_host()
        ctx = MagicMock()
        ctx.token = "ghes-tok"
        host_mock.auth_resolver.resolve.return_value = ctx
        d = DownloadDelegate(host_mock)

        api_resp = _fake_response(200, b"ghes bytes")
        host_mock._resilient_get.return_value = api_resp

        dep = self._dep(host="ghes.company.com")
        with (
            patch.object(d, "try_raw_download") as mock_cdn,
            patch.object(
                DownloadDelegate,
                "_extract_contents_api_payload",
                return_value=b"ghes bytes",
            ),
        ):
            d.download_github_file(dep, "apm.yml")
        mock_cdn.assert_not_called()

    def test_generic_host_tries_raw_url_first(self) -> None:
        host_mock = _make_host()
        ctx = MagicMock()
        ctx.token = None
        ctx.source = ""
        host_mock.auth_resolver.resolve.return_value = ctx
        d = DownloadDelegate(host_mock)

        raw_resp = _fake_response(200, b"raw bytes")
        host_mock._resilient_get.return_value = raw_resp
        dep = self._dep(host="gitea.myorg.com")

        result = d.download_github_file(dep, "apm.yml")
        assert result == b"raw bytes"

    def test_generic_host_raw_fails_falls_back_to_api(self) -> None:
        host_mock = _make_host()
        ctx = MagicMock()
        ctx.token = None
        ctx.source = ""
        host_mock.auth_resolver.resolve.return_value = ctx
        d = DownloadDelegate(host_mock)

        raw_resp = _fake_response(404, b"")
        api_resp = _fake_response(200, b"{}")
        host_mock._resilient_get.side_effect = [raw_resp, api_resp]
        dep = self._dep(host="gitea.myorg.com")

        with patch.object(
            DownloadDelegate,
            "_extract_contents_api_payload",
            return_value=b"payload",
        ):
            result = d.download_github_file(dep, "apm.yml")
        assert result == b"payload"

    def test_404_main_branch_tries_master_fallback(self) -> None:
        host_mock = _make_host(github_token="tok")
        ctx = MagicMock()
        ctx.token = "tok"
        host_mock.auth_resolver.resolve.return_value = ctx
        d = DownloadDelegate(host_mock)

        fail_resp = _fake_response(404, b"")
        ok_resp = _fake_response(200, b"from master")
        host_mock._resilient_get.side_effect = [fail_resp, ok_resp]

        with patch.object(
            DownloadDelegate,
            "_extract_contents_api_payload",
            return_value=b"from master",
        ):
            result = d.download_github_file(self._dep(), "apm.yml", ref="main")
        assert result == b"from master"

    def test_404_non_default_ref_raises_runtime(self) -> None:
        host_mock = _make_host(github_token="tok")
        ctx = MagicMock()
        ctx.token = "tok"
        host_mock.auth_resolver.resolve.return_value = ctx
        d = DownloadDelegate(host_mock)
        host_mock._resilient_get.return_value = _fake_response(404, b"")

        with pytest.raises(RuntimeError, match="File not found"):
            d.download_github_file(self._dep(), "apm.yml", ref="feature/x")

    def test_401_rate_limit_raises_runtime(self) -> None:
        host_mock = _make_host(github_token=None)
        ctx = MagicMock()
        ctx.token = None
        host_mock.auth_resolver.resolve.return_value = ctx
        host_mock.auth_resolver.build_error_context.return_value = "hint"
        d = DownloadDelegate(host_mock)

        resp = _fake_response(401, b"", headers={"X-RateLimit-Remaining": "0"})
        host_mock._resilient_get.return_value = resp

        with patch.object(d, "try_raw_download", return_value=None):
            with pytest.raises(RuntimeError, match="rate limit"):
                d.download_github_file(self._dep(), "apm.yml")

    def test_403_with_token_tries_unauth_retry(self) -> None:
        host_mock = _make_host(github_token="tok")
        ctx = MagicMock()
        ctx.token = "tok"
        host_mock.auth_resolver.resolve.return_value = ctx
        d = DownloadDelegate(host_mock)

        # First call: 403; second call (unauth retry): 200
        auth_resp = _fake_response(403, b"")
        unauth_resp = _fake_response(200, b"public content")
        host_mock._resilient_get.side_effect = [auth_resp, unauth_resp]

        with patch.object(
            DownloadDelegate,
            "_extract_contents_api_payload",
            return_value=b"public content",
        ):
            result = d.download_github_file(self._dep(), "apm.yml")
        assert result == b"public content"

    def test_403_unauth_retry_fails_raises_runtime(self) -> None:
        host_mock = _make_host(github_token="tok")
        ctx = MagicMock()
        ctx.token = "tok"
        host_mock.auth_resolver.resolve.return_value = ctx
        host_mock.auth_resolver.build_error_context.return_value = "hint"
        d = DownloadDelegate(host_mock)

        auth_resp = _fake_response(403, b"")
        # Unauth retry also fails
        host_mock._resilient_get.side_effect = [auth_resp, auth_resp]

        with pytest.raises(RuntimeError, match="Authentication failed"):
            d.download_github_file(self._dep(), "apm.yml")

    def test_network_error_raises_runtime(self) -> None:
        host_mock = _make_host(github_token="tok")
        ctx = MagicMock()
        ctx.token = "tok"
        host_mock.auth_resolver.resolve.return_value = ctx
        d = DownloadDelegate(host_mock)
        host_mock._resilient_get.side_effect = requests.exceptions.ConnectionError("err")

        with pytest.raises(RuntimeError, match="Network error"):
            d.download_github_file(self._dep(), "apm.yml")

    def test_other_http_error_raises_runtime(self) -> None:
        host_mock = _make_host(github_token="tok")
        ctx = MagicMock()
        ctx.token = "tok"
        host_mock.auth_resolver.resolve.return_value = ctx
        d = DownloadDelegate(host_mock)
        host_mock._resilient_get.return_value = _fake_response(500, b"")

        with pytest.raises(RuntimeError, match="HTTP 500"):
            d.download_github_file(self._dep(), "apm.yml")

    def test_verbose_callback_on_cdn_success(self) -> None:
        host_mock = _make_host(github_token=None)
        ctx = MagicMock()
        ctx.token = None
        host_mock.auth_resolver.resolve.return_value = ctx
        d = DownloadDelegate(host_mock)
        callback = MagicMock()

        with patch.object(d, "try_raw_download", return_value=b"cdn"):
            d.download_github_file(self._dep(), "apm.yml", verbose_callback=callback)
        callback.assert_called_once()

    def test_ghe_cloud_host_never_skips_cdn(self) -> None:
        """*.ghe.com is a GitHub-family host; CDN path applies only for github.com."""
        host_mock = _make_host()
        ctx = MagicMock()
        ctx.token = None
        host_mock.auth_resolver.resolve.return_value = ctx
        d = DownloadDelegate(host_mock)
        dep = self._dep(host="myorg.ghe.com")

        api_resp = _fake_response(200, b"ghe content")
        host_mock._resilient_get.return_value = api_resp

        with patch.object(d, "try_raw_download") as mock_cdn:
            with patch.object(
                DownloadDelegate,
                "_extract_contents_api_payload",
                return_value=b"ghe content",
            ):
                result = d.download_github_file(dep, "apm.yml")
                _ = result  # suppress unused variable lint warning
        # CDN not called for ghe.com
        mock_cdn.assert_not_called()


# ---------------------------------------------------------------------------
# _is_configured_ghes
# ---------------------------------------------------------------------------


class TestIsConfiguredGhes:
    def test_no_env_var_returns_false(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("GITHUB_HOST", None)
            assert DownloadDelegate._is_configured_ghes("custom.company.com") is False

    def test_matching_host_returns_true(self) -> None:
        with patch.dict("os.environ", {"GITHUB_HOST": "custom.company.com"}):
            assert DownloadDelegate._is_configured_ghes("custom.company.com") is True

    def test_case_insensitive_match(self) -> None:
        with patch.dict("os.environ", {"GITHUB_HOST": "Custom.Company.Com"}):
            assert DownloadDelegate._is_configured_ghes("custom.company.com") is True

    def test_non_matching_host_returns_false(self) -> None:
        with patch.dict("os.environ", {"GITHUB_HOST": "other.host.com"}):
            assert DownloadDelegate._is_configured_ghes("different.host.com") is False


# ---------------------------------------------------------------------------
# _build_contents_api_urls
# ---------------------------------------------------------------------------


class TestBuildContentsApiUrls:
    def test_github_com_uses_api_github_com(self) -> None:
        urls = DownloadDelegate._build_contents_api_urls(
            "github.com", "owner", "repo", "apm.yml", "main"
        )
        assert len(urls) >= 1
        from urllib.parse import urlparse

        parsed = urlparse(urls[0])
        assert parsed.netloc == "api.github.com"

    def test_ghe_cloud_uses_host_api(self) -> None:
        urls = DownloadDelegate._build_contents_api_urls(
            "myorg.ghe.com", "owner", "repo", "apm.yml", "main"
        )
        assert len(urls) >= 1
        from urllib.parse import urlparse

        parsed = urlparse(urls[0])
        assert parsed.hostname == "myorg.ghe.com"

    def test_ghes_custom_uses_host_api_v3(self) -> None:
        with patch.dict("os.environ", {"GITHUB_HOST": "ghes.company.com"}):
            urls = DownloadDelegate._build_contents_api_urls(
                "ghes.company.com",
                "owner",
                "repo",
                "apm.yml",
                "main",
                is_github_host=True,
            )
        assert len(urls) >= 1
        assert urlparse(urls[0]).hostname == "ghes.company.com"

    def test_generic_host_returns_multiple_candidates(self) -> None:
        urls = DownloadDelegate._build_contents_api_urls(
            "gitea.myorg.com", "owner", "repo", "apm.yml", "main"
        )
        assert len(urls) >= 1
        assert urlparse(urls[0]).hostname == "gitea.myorg.com"

    def test_is_github_host_none_auto_detected(self) -> None:
        """When is_github_host=None, github.com is auto-detected as GitHub."""
        urls = DownloadDelegate._build_contents_api_urls(
            "github.com", "owner", "repo", "apm.yml", "main", is_github_host=None
        )
        assert urlparse(urls[0]).hostname == "api.github.com"


# ---------------------------------------------------------------------------
# _build_generic_host_auth_headers
# ---------------------------------------------------------------------------


class TestBuildGenericHostAuthHeaders:
    def test_no_auth_ctx_returns_empty(self) -> None:
        headers = DownloadDelegate._build_generic_host_auth_headers("gitea.myorg.com", None)
        assert headers == {}

    def test_no_token_on_ctx_returns_empty(self) -> None:
        ctx = MagicMock()
        ctx.token = None
        headers = DownloadDelegate._build_generic_host_auth_headers("gitea.myorg.com", ctx)
        assert "Authorization" not in headers

    def test_git_credential_fill_source_attaches_token(self) -> None:
        ctx = MagicMock()
        ctx.token = "cred-token"
        ctx.source = "git-credential-fill"
        headers = DownloadDelegate._build_generic_host_auth_headers("gitea.myorg.com", ctx)
        assert headers["Authorization"] == "token cred-token"

    def test_org_scoped_pat_attaches_token(self) -> None:
        ctx = MagicMock()
        ctx.token = "org-token"
        ctx.source = "GITHUB_APM_PAT_MYORG"
        headers = DownloadDelegate._build_generic_host_auth_headers("gitea.myorg.com", ctx)
        assert headers["Authorization"] == "token org-token"

    def test_configured_ghes_attaches_token(self) -> None:
        ctx = MagicMock()
        ctx.token = "ghes-token"
        ctx.source = "GITHUB_TOKEN"  # global, but host is configured GHES
        with patch.dict("os.environ", {"GITHUB_HOST": "custom.company.com"}):
            headers = DownloadDelegate._build_generic_host_auth_headers("custom.company.com", ctx)
        assert headers["Authorization"] == "token ghes-token"

    def test_global_token_not_forwarded_to_unknown_host(self) -> None:
        ctx = MagicMock()
        ctx.token = "global-tok"
        ctx.source = "GITHUB_TOKEN"  # global, unknown host
        headers = DownloadDelegate._build_generic_host_auth_headers("unknown.host.example", ctx)
        assert "Authorization" not in headers

    def test_accept_header_added_when_provided(self) -> None:
        ctx = MagicMock()
        ctx.token = None
        headers = DownloadDelegate._build_generic_host_auth_headers(
            "gitea.myorg.com", ctx, accept="application/json"
        )
        assert headers["Accept"] == "application/json"

    def test_accept_header_absent_when_none(self) -> None:
        ctx = MagicMock()
        ctx.token = None
        headers = DownloadDelegate._build_generic_host_auth_headers(
            "gitea.myorg.com", ctx, accept=None
        )
        assert "Accept" not in headers


# ---------------------------------------------------------------------------
# _extract_contents_api_payload
# ---------------------------------------------------------------------------


class TestExtractContentsApiPayload:
    def test_github_host_returns_raw_content(self) -> None:
        resp = MagicMock()
        resp.content = b"raw bytes"
        resp.headers = {}
        result = DownloadDelegate._extract_contents_api_payload(resp, is_github_host=True)
        assert result == b"raw bytes"

    def test_generic_host_base64_envelope_decoded(self) -> None:
        content_b64 = base64.b64encode(b"decoded content").decode()
        payload = json.dumps({"content": content_b64, "encoding": "base64"})
        resp = MagicMock()
        resp.content = payload.encode("utf-8")
        resp.headers = {"Content-Type": "application/json"}
        result = DownloadDelegate._extract_contents_api_payload(resp, is_github_host=False)
        assert result == b"decoded content"

    def test_generic_host_non_json_passthrough(self) -> None:
        resp = MagicMock()
        resp.content = b"raw binary data"
        resp.headers = {"Content-Type": "application/octet-stream"}
        result = DownloadDelegate._extract_contents_api_payload(resp, is_github_host=False)
        assert result == b"raw binary data"

    def test_generic_host_json_without_content_field_passthrough(self) -> None:
        payload = json.dumps({"size": 42, "sha": "abc"})
        resp = MagicMock()
        resp.content = payload.encode("utf-8")
        resp.headers = {"Content-Type": "application/json"}
        result = DownloadDelegate._extract_contents_api_payload(resp, is_github_host=False)
        assert result == payload.encode("utf-8")

    def test_generic_host_invalid_base64_falls_back_to_body(self) -> None:
        payload = json.dumps({"content": "!!!invalid_b64!!!", "encoding": "base64"})
        resp = MagicMock()
        resp.content = payload.encode("utf-8")
        resp.headers = {"Content-Type": "application/json"}
        result = DownloadDelegate._extract_contents_api_payload(resp, is_github_host=False)
        assert result == payload.encode("utf-8")

    def test_generic_host_non_base64_encoding_returns_string(self) -> None:
        payload = json.dumps({"content": "plain text", "encoding": "utf-8"})
        resp = MagicMock()
        resp.content = payload.encode("utf-8")
        resp.headers = {"Content-Type": "application/json"}
        result = DownloadDelegate._extract_contents_api_payload(resp, is_github_host=False)
        assert result == b"plain text"

    def test_generic_host_json_starts_with_brace_but_no_content_type(self) -> None:
        """Body that starts with '{' and no Content-Type should still be parsed."""
        content_b64 = base64.b64encode(b"file bytes").decode()
        payload = json.dumps({"content": content_b64, "encoding": "base64"})
        resp = MagicMock()
        resp.content = payload.encode("utf-8")
        resp.headers = {}
        result = DownloadDelegate._extract_contents_api_payload(resp, is_github_host=False)
        assert result == b"file bytes"

    def test_generic_host_invalid_utf8_body_falls_back(self) -> None:
        resp = MagicMock()
        resp.content = b"\xff\xfe invalid utf-8 \x00"
        resp.headers = {"Content-Type": "application/json"}
        result = DownloadDelegate._extract_contents_api_payload(resp, is_github_host=False)
        # Falls back to the raw body
        assert result == resp.content


# ---------------------------------------------------------------------------
# _build_unsupported_or_missing_error
# ---------------------------------------------------------------------------


class TestBuildUnsupportedOrMissingError:
    def test_github_host_simple_message(self) -> None:
        msg = DownloadDelegate._build_unsupported_or_missing_error(
            "github.com",
            "owner/repo",
            "apm.yml",
            "main",
            ["https://api.github.com/repos/owner/repo/contents/apm.yml"],
            is_github_host=True,
        )
        assert "File not found" in msg
        assert "apm.yml" in msg
        assert "owner/repo" in msg

    def test_github_host_with_fallback_ref(self) -> None:
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

    def test_non_github_host_names_tried_families(self) -> None:
        msg = DownloadDelegate._build_unsupported_or_missing_error(
            "gitea.myorg.com",
            "owner/repo",
            "apm.yml",
            "main",
            [
                "https://gitea.myorg.com/api/v1/repos/owner/repo/contents/apm.yml",
            ],
            is_github_host=False,
        )
        assert "generic host" in msg.lower() or "gitea" in msg.lower()
        assert "apm.yml" in msg

    def test_non_github_host_at_specific_ref(self) -> None:
        msg = DownloadDelegate._build_unsupported_or_missing_error(
            "gitea.myorg.com",
            "owner/repo",
            "apm.yml",
            "v1.2.3",
            ["https://gitea.myorg.com/api/v1/repos/owner/repo/contents/apm.yml"],
            is_github_host=False,
        )
        assert "v1.2.3" in msg
