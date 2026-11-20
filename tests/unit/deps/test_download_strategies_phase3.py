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
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
import requests

from apm_cli.deps.download_strategies import DownloadDelegate
from apm_cli.deps.git_file_transport import GitFileTransportError
from apm_cli.deps.transport_selection import (
    NoOpInsteadOfResolver,
    ProtocolPreference,
    TransportSelector,
)
from apm_cli.models.apm_package import DependencyReference
from tests.utils.simulated_ado_chain import install_simulated_ado_chain

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
    resolved_token = ado_token if ado_token is not None else github_token
    ctx.token = resolved_token
    ctx.source = (
        "ADO_APM_PAT" if ado_token is not None else ("GITHUB_APM_PAT_ORG" if github_token else "")
    )
    ctx.auth_scheme = "basic"
    ctx.git_env = {}
    auth.resolve.return_value = ctx
    auth.resolve_for_dep.return_value = ctx
    install_simulated_ado_chain(auth)
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


# ---------------------------------------------------------------------------
# resilient_get
# ---------------------------------------------------------------------------


class TestResilientGet:
    """Tests for DownloadDelegate.resilient_get covering all retry branches."""

    def setup_method(self) -> None:
        self.delegate = DownloadDelegate(_make_host())

    @pytest.mark.parametrize(
        "failures",
        [
            [_fake_response(503, b"", headers={})],
            [requests.exceptions.ConnectionError("reset")],
        ],
        ids=["503", "connection-error"],
    )
    def test_redirect_base_attempts_keep_retry_semantics(self, failures) -> None:
        """Owner-routed attempts still retry transient failures, one hop each."""
        url = "https://dev.azure.com/o/p/_apis/git/repositories/r/items"
        ok_resp = _fake_response(200, b"ok")
        with (
            patch(
                "apm_cli.utils.http_credentials.CredentialScopedSession.request",
                side_effect=[*failures, ok_resp],
            ) as request,
            patch("time.sleep"),
        ):
            result = self.delegate.resilient_get(
                url, {"Authorization": "Bearer t"}, max_retries=3, redirect_base=url
            )
        assert result is ok_resp
        assert request.call_count == 2
        assert all(call.kwargs["allow_redirects"] is False for call in request.call_args_list)
        assert all(call.kwargs.get("auth") is not None for call in request.call_args_list)

    def test_rate_limit_returns_first_response_without_retry(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A throttle never spends a retry budget."""
        rate_resp = _fake_response(429, b"rate", headers={"Retry-After": "0.001"})
        with (
            patch("requests.get", return_value=rate_resp),
            patch("time.sleep") as mock_sleep,
            patch.dict("os.environ", {"APM_DEBUG": "1"}),
        ):
            result = self.delegate.resilient_get(
                "https://example.com", {}, max_retries=2, retry_throttles=False
            )
        assert result is rate_resp
        mock_sleep.assert_not_called()
        captured = capsys.readouterr()
        assert "retry in" not in captured.err


# ---------------------------------------------------------------------------
# build_repo_url
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_artifactory_headers
# ---------------------------------------------------------------------------


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
        assert host._resilient_get.call_args.kwargs["allow_netrc"] is False
        assert "allow_redirects" not in host._resilient_get.call_args.kwargs


# ---------------------------------------------------------------------------
# download_file_from_artifactory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# try_raw_download
# ---------------------------------------------------------------------------


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

    def test_404_fallback_preserves_custom_port(self) -> None:
        host = _make_host(ado_token="pat")
        d = DownloadDelegate(host)
        host._resilient_get.side_effect = [
            _fake_response(404, b""),
            _fake_response(200, b"fallback content"),
        ]
        result = d.download_ado_file(
            self._dep(host="ado.example.test", port=8443),
            "apm.yml",
            ref="main",
        )
        assert result == b"fallback content"
        parsed_urls = [urlparse(call.args[0]) for call in host._resilient_get.call_args_list]
        assert [(parsed.hostname, parsed.port) for parsed in parsed_urls] == [
            ("ado.example.test", 8443),
            ("ado.example.test", 8443),
        ]

    def test_no_ado_token_no_auth_header(self) -> None:
        host = _make_host(ado_token=None)
        d = DownloadDelegate(host)
        resp = _fake_response(200, b"ok")
        host._resilient_get.return_value = resp
        # Simulate auth_resolver.resolve() returning no token (no PAT, no bearer)
        ctx = MagicMock()
        ctx.token = None
        ctx.auth_scheme = "basic"
        host.auth_resolver.resolve.return_value = ctx
        d.download_ado_file(self._dep(), "apm.yml")
        call_kwargs = host._resilient_get.call_args[1]
        headers = call_kwargs.get("headers", {})
        assert "Authorization" not in headers

    # ------------------------------------------------------------------
    # Regression tests for #1671: HTML sign-in page + bearer fallback
    # ------------------------------------------------------------------

    def test_html_200_response_raises_actionable_error(self) -> None:
        """Regression #1671: ADO returns 200 + text/html when unauth; must fail-closed."""
        host = _make_host(ado_token=None)
        ctx = MagicMock()
        ctx.token = None
        ctx.auth_scheme = "basic"
        host.auth_resolver.resolve.return_value = ctx
        host.auth_resolver.build_error_context.return_value = "Set ADO_APM_PAT or run 'az login'."
        html_body = b"<!DOCTYPE html><html><head><title>Azure DevOps Services | Sign In</title></head></html>"
        resp = _fake_response(200, html_body, headers={"Content-Type": "text/html; charset=utf-8"})
        host._resilient_get.return_value = resp
        d = DownloadDelegate(host)
        with pytest.raises(RuntimeError, match="sign-in page"):
            d.download_ado_file(self._dep(), "apm.yml")

    def test_html_203_response_is_rejected(self) -> None:
        host = _make_host(ado_token=None)
        host.auth_resolver.resolve.return_value = MagicMock(
            token=None,
            auth_scheme="basic",
            source="none",
        )
        host.auth_resolver.build_error_context.return_value = "Set ADO_APM_PAT."
        host._resilient_get.return_value = _fake_response(
            203,
            b"<html>Sign In</html>",
            headers={"Content-Type": "text/html"},
        )
        with pytest.raises(RuntimeError, match="sign-in page"):
            DownloadDelegate(host).download_ado_file(self._dep(), "apm.yml")

    def test_non_200_success_status_is_rejected(self) -> None:
        host = _make_host(ado_token="pat")
        host._resilient_get.return_value = _fake_response(
            206,
            b"partial",
            headers={"Content-Type": "application/octet-stream"},
        )
        with pytest.raises(RuntimeError, match="Unexpected HTTP 206"):
            DownloadDelegate(host).download_ado_file(self._dep(), "apm.yml")

    def test_html_200_response_writes_no_content(self) -> None:
        """Mutation break: HTML sign-in detection must prevent content from being returned."""
        host = _make_host(ado_token=None)
        ctx = MagicMock()
        ctx.token = None
        ctx.auth_scheme = "basic"
        host.auth_resolver.resolve.return_value = ctx
        host.auth_resolver.build_error_context.return_value = "Set ADO_APM_PAT."
        html_body = b"<html><body>Sign In</body></html>"
        resp = _fake_response(200, html_body, headers={"Content-Type": "text/html"})
        host._resilient_get.return_value = resp
        d = DownloadDelegate(host)
        # Assertions are placed OUTSIDE the pytest.raises block so they execute
        # in the non-raising path; if the guard is removed, download_ado_file
        # returns html_body and the assert below catches the regression.
        raised = False
        result = None
        try:
            result = d.download_ado_file(self._dep(), "apm.yml")
        except RuntimeError:
            raised = True
        assert raised, "Expected RuntimeError for HTML sign-in page; guard may have been removed"
        assert result is None, "Bug: HTML sign-in page was returned as file content"

    def test_bearer_fallback_used_when_no_pat(self) -> None:
        """Regression #1671: bearer token from auth_resolver used when no PAT present."""
        host = _make_host(ado_token=None)
        ctx = MagicMock()
        ctx.token = "aad-bearer-jwt"
        ctx.auth_scheme = "bearer"
        host.auth_resolver.resolve.return_value = ctx
        resp = _fake_response(
            200, b"agent content", headers={"Content-Type": "application/octet-stream"}
        )
        host._resilient_get.return_value = resp
        d = DownloadDelegate(host)
        result = d.download_ado_file(self._dep(), "apm.yml")
        assert result == b"agent content"
        call_kwargs = host._resilient_get.call_args[1]
        headers = call_kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer aad-bearer-jwt"

    def test_pat_path_unchanged_when_pat_present(self) -> None:
        """PAT must remain the primary auth path; bearer is strictly the fallback."""
        host = _make_host(ado_token="my-secret-pat")
        resp = _fake_response(
            200, b"file bytes", headers={"Content-Type": "application/octet-stream"}
        )
        host._resilient_get.return_value = resp
        d = DownloadDelegate(host)
        result = d.download_ado_file(self._dep(), "apm.yml")
        assert result == b"file bytes"
        call_kwargs = host._resilient_get.call_args[1]
        headers = call_kwargs.get("headers", {})
        # PAT is used as Basic auth, bearer is NOT used
        assert "Authorization" in headers
        expected_auth = base64.b64encode(b":my-secret-pat").decode()
        assert expected_auth in headers["Authorization"]
        host.auth_resolver.resolve.assert_called_once_with(
            "dev.azure.com",
            "my-org",
            port=None,
        )

    def test_pat_resolution_routes_through_auth_resolver(self) -> None:
        """The REST PAT path must consume the canonical AuthResolver context."""
        host = _make_host(ado_token="pat-value")
        resp = _fake_response(200, b"data", headers={"Content-Type": "application/json"})
        host._resilient_get.return_value = resp
        d = DownloadDelegate(host)
        d.download_ado_file(self._dep(), "apm.yml")
        host.auth_resolver.resolve.assert_called_once_with(
            "dev.azure.com",
            "my-org",
            port=None,
        )

    def test_rejected_services_pat_retries_with_bearer(self) -> None:
        """A REST 401 uses AuthResolver's canonical PAT-to-bearer protocol."""
        host = _make_host(ado_token="stale-pat")
        host._resilient_get.side_effect = [
            _fake_response(401, b""),
            _fake_response(
                200,
                b"file bytes",
                headers={"Content-Type": "application/octet-stream"},
            ),
        ]

        provider = MagicMock()
        provider.is_available.return_value = True
        provider.get_bearer_token.return_value = "fresh-bearer"
        host.auth_resolver.bearer_provider = provider
        result = DownloadDelegate(host).download_ado_file(
            self._dep(),
            "apm.yml",
        )
        assert result == b"file bytes"
        retry_headers = host._resilient_get.call_args_list[1].kwargs["headers"]
        scheme, credential = retry_headers["Authorization"].split(" ", 1)
        assert scheme == "Bearer"
        assert credential == "fresh-bearer"

    def test_html_detection_on_fallback_ref_response(self) -> None:
        """HTML sign-in detection fires on the main/master fallback response too."""
        host = _make_host(ado_token=None)
        ctx = MagicMock()
        ctx.token = None
        ctx.auth_scheme = "basic"
        host.auth_resolver.resolve.return_value = ctx
        host.auth_resolver.build_error_context.return_value = "Set ADO_APM_PAT."
        fail_resp = _fake_response(404, b"")
        html_resp = _fake_response(
            200,
            b"<html>Sign In</html>",
            headers={"Content-Type": "text/html"},
        )
        host._resilient_get.side_effect = [fail_resp, html_resp]
        d = DownloadDelegate(host)
        with pytest.raises(RuntimeError, match="sign-in page"):
            d.download_ado_file(self._dep(), "apm.yml", ref="main")

    def test_404_with_html_content_type_does_not_trigger_sign_in_detection(self) -> None:
        """Regression lock: 404 response with text/html body must NOT fire the HTML sign-in guard.

        ADO can return 404 pages as text/html.  Sign-in detection must only fire
        on 200 responses so 404s fall through to raise_for_status and then to the
        main/master ref fallback path rather than raising a misleading 'sign-in page'
        error.  This test proves the status-code guard added in the HTML detection fix.
        """
        host = _make_host(ado_token=None)
        ctx = MagicMock()
        ctx.token = None
        ctx.auth_scheme = "basic"
        host.auth_resolver.resolve.return_value = ctx
        # First call: 404 with text/html (an ADO error page)
        # Second call (fallback): 200 with real content
        html_404 = _fake_response(
            404, b"<html>Not Found</html>", headers={"Content-Type": "text/html"}
        )
        ok_resp = _fake_response(
            200, b"file-bytes", headers={"Content-Type": "application/octet-stream"}
        )
        host._resilient_get.side_effect = [html_404, ok_resp]
        d = DownloadDelegate(host)
        # Should succeed via the main->master fallback, NOT raise "sign-in page"
        result = d.download_ado_file(self._dep(), "apm.yml", ref="main")
        assert result == b"file-bytes"
        assert host._resilient_get.call_count == 2

    def test_basic_context_uses_ado_pat_header(self) -> None:
        """A basic AuthResolver context produces an ADO PAT header."""
        host = _make_host(ado_token=None)
        ctx = MagicMock()
        ctx.token = "some-token"
        ctx.source = "ADO_APM_PAT"
        ctx.auth_scheme = "basic"  # non-bearer scheme -> must not inject as Bearer
        host.auth_resolver.resolve.return_value = ctx
        resp = _fake_response(200, b"content", headers={"Content-Type": "application/octet-stream"})
        host._resilient_get.return_value = resp
        d = DownloadDelegate(host)
        d.download_ado_file(self._dep(), "apm.yml")
        call_kwargs = host._resilient_get.call_args[1]
        headers = call_kwargs.get("headers", {})
        scheme, credential = headers["Authorization"].split(" ", 1)
        assert scheme == "Basic"
        assert base64.b64decode(credential).decode() == ":some-token"


# ---------------------------------------------------------------------------
# download_gitlab_file
# ---------------------------------------------------------------------------


class TestDownloadGitlabFile:
    @pytest.fixture(autouse=True)
    def _git_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exercise REST recovery after a selected HTTPS Git attempt fails."""
        monkeypatch.setattr(
            DownloadDelegate,
            "_download_gitlab_file_via_git",
            MagicMock(side_effect=GitFileTransportError("git transport unavailable")),
        )
        monkeypatch.setattr(
            "apm_cli.deps.download_strategies.validate_git_url_rewrite_safety",
            lambda requested_url, _env: requested_url,
        )

    def _dep(self) -> DependencyReference:
        return DependencyReference(
            repo_url="mygroup/myproject",
            host="gitlab.example.com",
        )

    def _setup_host_info(self, host_mock: MagicMock) -> None:
        info = MagicMock()
        info.kind = "gitlab"
        info.api_base = "https://gitlab.example.com/api/v4"
        host_mock.auth_resolver.classify_host.return_value = info
        ctx = MagicMock()
        ctx.token = "gl-tok"
        host_mock.auth_resolver.resolve.return_value = ctx
        host_mock.auth_resolver.resolve_for_remote.return_value = ctx
        host_mock.auth_resolver.git_env_for_remote.return_value = {}
        host_mock.auth_resolver.build_native_git_credential_env.return_value = {}
        host_mock._protocol_pref = ProtocolPreference.NONE
        host_mock._allow_fallback = False
        host_mock._transport_selector = TransportSelector(NoOpInsteadOfResolver())

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
        self._setup_host_info(host)
        host.auth_resolver.resolve.return_value.token = None
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

    def test_verbose_callback_called_on_success(self) -> None:
        host = _make_host()
        self._setup_host_info(host)
        d = DownloadDelegate(host)
        resp = _fake_response(200, b"data")
        host._resilient_get.return_value = resp
        callback = MagicMock()

        with patch(
            "apm_cli.deps.download_strategies.AuthResolver.gitlab_rest_headers",
            return_value={},
        ):
            d.download_gitlab_file(self._dep(), "apm.yml", verbose_callback=callback)
        # The mocked git failure drives the REST fallback path without spawning
        # subprocess/network work. Verbose output stays transport-agnostic.
        assert callback.called
        assert any("Downloaded file:" in str(c.args[0]) for c in callback.call_args_list)


# ---------------------------------------------------------------------------
# download_github_file
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _is_configured_ghes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _build_contents_api_urls
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _build_generic_host_auth_headers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _extract_contents_api_payload
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _build_unsupported_or_missing_error
# ---------------------------------------------------------------------------


def test_ado_download_sign_in_redirect_is_classified_not_followed() -> None:
    """A sign-in 302 reaches the auth chain as auth_required; it is never followed."""
    from apm_cli.core.auth import AuthResolver
    from apm_cli.core.auth_outcome import AuthAttemptFailed, AuthOutcomeKind, classify_auth_outcome

    host = _make_host()
    delegate = DownloadDelegate(host)
    host._resilient_get.side_effect = delegate.resilient_get
    host.auth_resolver.http_authorization_headers.side_effect = (
        AuthResolver.http_authorization_headers
    )
    outcomes: list[AuthOutcomeKind] = []

    def _chain(_host, operation, **_kwargs):
        try:
            return operation(None, {})
        except AuthAttemptFailed as failed:
            outcome = classify_auth_outcome(host_kind="ado", response=failed.response)
            outcomes.append(outcome.kind)
            raise

    host.auth_resolver.try_with_fallback.side_effect = _chain
    sign_in = MagicMock(status_code=302)
    sign_in.headers = {"Location": "https://app.vssps.visualstudio.com/_signin?realm=x"}
    dep = MagicMock(
        host="dev.azure.com",
        port=None,
        ado_organization="org",
        ado_project="proj",
        ado_repo="repo",
        repo_url="org/proj/_git/repo",
    )

    with (
        patch(
            "apm_cli.utils.http_credentials.CredentialScopedSession.request",
            return_value=sign_in,
        ) as request,
        pytest.raises(RuntimeError, match="HTTP 302"),
    ):
        delegate.download_ado_file(dep, "apm.yml", ref="feature")

    assert outcomes == [AuthOutcomeKind.AUTH_REQUIRED]
    assert request.call_count == 1
    assert request.call_args.kwargs["allow_redirects"] is False
    assert urlparse(request.call_args.kwargs["url"]).hostname == "dev.azure.com"
    assert request.call_args.kwargs.get("auth") is not None
