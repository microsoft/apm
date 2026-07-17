"""Focused contracts for the virtual-file GitHub throttle fallback."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.deps.download_strategies import DownloadDelegate
from apm_cli.deps.git_file_transport import GitFileFetchResult, GitFileTransportError
from apm_cli.deps.github_rate_limit import GitHubThrottle, GitHubThrottleError
from apm_cli.models.apm_package import DependencyReference

_SHA = "0123456789abcdef0123456789abcdef01234567"


def _response(status_code: int, headers: dict[str, str] | None = None) -> MagicMock:
    """Build an HTTP response whose status check raises for failures."""
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.content = b""
    response.raise_for_status.side_effect = (
        requests.exceptions.HTTPError(response=response) if status_code >= 400 else None
    )
    return response


def _host(token: str | None) -> MagicMock:
    """Build the dependency-owned services needed by DownloadDelegate."""
    host = MagicMock()
    host.github_token = token
    host.ado_token = None
    host.artifactory_token = None
    host.github_host = "github.com"
    host.git_env = {"UNRELATED_DOWNLOADER_TOKEN": "must-not-be-used"}
    host._build_noninteractive_git_env.return_value = {"NORMAL_GIT": "1"}
    context = MagicMock()
    context.token = token
    context.auth_scheme = "basic"
    context.git_env = {"GIT_TOKEN": token} if token else {}
    host._resolve_dep_auth_ctx.return_value = context
    host._resolve_dep_token.return_value = token
    host.auth_resolver.resolve_for_dep.return_value = context
    host.auth_resolver.classify_host.return_value = MagicMock(
        kind="github",
        api_base="https://api.github.com",
    )
    return host


def _dep() -> DependencyReference:
    """Build one GitHub virtual-file dependency."""
    return DependencyReference(
        repo_url="owner/repo",
        host="github.com",
        reference="main",
        virtual_path="instructions/example.instructions.md",
        is_virtual=True,
    )


@pytest.mark.parametrize(
    ("status_code", "headers"),
    (
        (429, {}),
        (403, {"X-RateLimit-Remaining": "0"}),
        (403, {"Retry-After": "60"}),
    ),
)
def test_confirmed_throttle_is_typed_before_any_fallback(
    status_code: int,
    headers: dict[str, str],
) -> None:
    """Every positive classifier branch reaches the one explicit fallback gate."""
    host = _host(token="private-token")
    host._resilient_get.return_value = _response(status_code, headers)
    delegate = DownloadDelegate(host)

    with pytest.raises(GitHubThrottleError) as exc_info:
        delegate.download_github_file(_dep(), "instructions/example.instructions.md", "main")

    assert host._resilient_get.call_args.kwargs["retry_throttles"] is False
    with patch.object(
        delegate,
        "_download_github_file_via_git",
        return_value=GitFileFetchResult(b"content", _SHA),
    ) as sparse_fetch:
        result = delegate.download_github_file_via_throttle_fallback(
            _dep(),
            "instructions/example.instructions.md",
            "main",
            exc_info.value,
        )

    assert result == GitFileFetchResult(b"content", _SHA)
    sparse_fetch.assert_called_once()


@pytest.mark.parametrize(
    ("status_code", "headers"),
    (
        (401, {"X-RateLimit-Remaining": "0"}),
        (403, {}),
        (403, {"X-RateLimit-Remaining": "1"}),
        (403, {"X-RateLimit-Remaining": "invalid"}),
        (404, {"X-RateLimit-Remaining": "0"}),
    ),
)
def test_auth_and_missing_responses_never_select_sparse_git(
    status_code: int,
    headers: dict[str, str],
) -> None:
    """Only a typed throttle can reach sparse Git; errors stay on their own path."""
    host = _host(token="private-token")
    host._resilient_get.return_value = _response(status_code, headers)
    delegate = DownloadDelegate(host)

    with (
        patch.object(delegate, "download_github_file_via_throttle_fallback") as fallback,
        pytest.raises(RuntimeError),
    ):
        delegate.download_github_file(_dep(), "instructions/example.instructions.md", "main")

    fallback.assert_not_called()


def test_throttle_on_default_branch_fallback_remains_typed() -> None:
    """A throttled master fallback must still reach the sparse-Git decision gate."""
    host = _host(token="private-token")
    host._resilient_get.side_effect = [
        _response(404),
        _response(429),
    ]
    delegate = DownloadDelegate(host)

    with pytest.raises(GitHubThrottleError):
        delegate.download_github_file(_dep(), "instructions/example.instructions.md", "main")

    assert host._resilient_get.call_count == 2
    assert all(
        call.kwargs["retry_throttles"] is False for call in host._resilient_get.call_args_list
    )


def test_private_fallback_uses_only_auth_resolver_git_environment() -> None:
    """Private fallback has a token-free URL and the resolver-owned Git env."""
    host = _host(token="private-token")
    delegate = DownloadDelegate(host)
    transport = MagicMock()
    transport.fetch_file_with_commit.return_value = GitFileFetchResult(b"content", _SHA)

    with (
        patch(
            "apm_cli.deps.download_strategies.GitSparseFileTransport",
            return_value=transport,
        ) as transport_factory,
        patch.object(
            delegate, "build_repo_url", return_value="https://github.com/owner/repo.git"
        ) as url,
    ):
        result = delegate.download_github_file_via_throttle_fallback(
            _dep(),
            "instructions/example.instructions.md",
            "main",
            GitHubThrottleError(GitHubThrottle(429, "http-429"), "github.com"),
        )
        build_url = transport_factory.call_args.kwargs["build_repo_url_fn"]
        assert build_url("owner/repo", dep_ref=_dep()) == "https://github.com/owner/repo.git"

    assert result.resolved_commit == _SHA
    git_env = transport_factory.call_args.kwargs["git_env"]
    assert git_env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert git_env["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer private-token"
    assert "GIT_TOKEN" not in git_env
    assert url.call_args.kwargs["token"] == ""


def test_public_fallback_uses_normal_git_environment() -> None:
    """Public fallback excludes downloader auth state and uses normal Git."""
    host = _host(token=None)
    delegate = DownloadDelegate(host)
    transport = MagicMock()
    transport.fetch_file_with_commit.return_value = GitFileFetchResult(b"content", _SHA)

    with patch(
        "apm_cli.deps.download_strategies.GitSparseFileTransport",
        return_value=transport,
    ) as transport_factory:
        delegate.download_github_file_via_throttle_fallback(
            _dep(),
            "instructions/example.instructions.md",
            "main",
            GitHubThrottleError(GitHubThrottle(429, "http-429"), "github.com"),
        )

    host._build_noninteractive_git_env.assert_called_once_with()
    assert transport_factory.call_args.kwargs["git_env"] == {"NORMAL_GIT": "1"}


def test_sparse_transport_failure_reports_the_throttle_and_transport_error() -> None:
    """The fallback never downgrades a confirmed throttle to generic inaccessible."""
    delegate = DownloadDelegate(_host(token=None))
    throttle = GitHubThrottleError(GitHubThrottle(429, "http-429"), "github.com")

    with patch.object(
        delegate,
        "_download_github_file_via_git",
        side_effect=GitFileTransportError("git file fetch failed: https://***@github.com"),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            delegate.download_github_file_via_throttle_fallback(
                _dep(),
                "instructions/example.instructions.md",
                "main",
                throttle,
            )

    message = str(exc_info.value)
    assert "GitHub API throttle" in message
    assert "sparse Git transport failed" in message
    assert "private-token" not in message
