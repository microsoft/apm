"""Git fallback after a GitHub throttle keeps its own failure chain."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.core.auth import AuthResolver
from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.deps.git_file_transport import GitFileFetchResult, GitFileTransportError
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.github_rate_limit import GitHubThrottle, GitHubThrottleError
from apm_cli.models.apm_package import DependencyReference

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-github-api-throttle-classification"),
]

_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize("credential_accepted", (True, False))
def test_private_file_throttle_then_distinct_git_auth_challenge(
    tmp_path: Path, credential_accepted: bool
) -> None:
    """The composed downloader retries Git, not the exhausted anonymous API."""
    manager = MagicMock(spec=GitHubTokenManager)
    manager.setup_environment.return_value = {}
    manager.get_token_for_purpose.return_value = None
    manager.resolve_credential_from_gh_cli.return_value = "fixture-gh-token"
    resolver = AuthResolver(token_manager=manager)
    dep = _dep()
    dep.reference = _SHA
    attempts: list[dict[str, str]] = []
    payload = b"---\napplyTo: '**'\ndescription: Private instruction\n---\n# Private\n"

    def transport_factory(*_args: object, **kwargs: object) -> MagicMock:
        env = kwargs["git_env"]
        assert isinstance(env, dict)
        attempts.append(env)
        transport = MagicMock()
        if len(attempts) == 1 or not credential_accepted:
            transport.fetch_file_with_commit.side_effect = GitFileTransportError(
                "git file fetch failed: fatal: Authentication failed"
            )
        else:
            transport.fetch_file_with_commit.return_value = GitFileFetchResult(payload, _SHA)
        return transport

    def response(url: str, **kwargs: object) -> MagicMock:
        from urllib.parse import urlparse

        assert "Authorization" not in kwargs.get("headers", {})
        if urlparse(url).hostname == "raw.githubusercontent.com":
            return _response(404)
        assert urlparse(url).hostname == "api.github.com"
        return _response(403, {"X-RateLimit-Remaining": "0"})

    with (
        patch.dict(os.environ, {}, clear=True),
        patch("requests.get", side_effect=response) as get,
    ):
        downloader = GitHubPackageDownloader(auth_resolver=resolver)
        downloader._strategies._git_file_transport_factory = transport_factory
        if credential_accepted:
            package = downloader.download_virtual_file_package(dep, tmp_path / "package")
            assert package.resolved_reference.resolved_commit == _SHA
            assert (
                tmp_path / "package/.apm/instructions/example.instructions.md"
            ).read_bytes() == payload
        else:
            with pytest.raises(RuntimeError, match="sparse Git transport failed") as error:
                downloader.download_virtual_file_package(dep, tmp_path / "package")
            assert "GitHub API throttle" in str(error.value)
            assert "Authentication failed" in str(error.value)
            assert isinstance(error.value.__cause__, GitFileTransportError)
            assert not (tmp_path / "package").exists()

    assert get.call_count == 2
    assert len(attempts) == 2
    assert _git_config_entries(attempts[0])["http.extraheader"] == [""]
    _assert_github_basic_header(
        _git_config_entries(attempts[1])["http.extraheader"][-1], "fixture-gh-token"
    )
    manager.resolve_credential_from_gh_cli.assert_called_once_with("github.com")
    manager.resolve_credential_from_git.assert_not_called()


@pytest.mark.parametrize("failure_kind", ("wrapped-throttle", "implicit-throttle", "network"))
def test_new_git_attempt_preserves_its_own_non_auth_failure_chain(
    tmp_path: Path, failure_kind: str
) -> None:
    """A genuine Git-attempt throttle or network cause must still block auth."""
    resolver = AuthResolver()
    dep = _dep()
    dep.reference = _SHA
    throttle = GitHubThrottleError(GitHubThrottle(429, "http-429"), "github.com")
    cause = (
        GitHubThrottleError(GitHubThrottle(403, "remaining-zero"), "github.com")
        if failure_kind != "network"
        else requests.exceptions.ConnectionError("could not resolve host")
    )

    def fail(_path: str) -> None:
        try:
            raise cause
        except Exception:
            if failure_kind == "implicit-throttle":
                raise GitFileTransportError("Authentication failed") from None
            raise GitFileTransportError("Authentication failed") from cause

    transport = MagicMock()
    transport.fetch_file_with_commit.side_effect = fail
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(resolver, "resolve") as resolve,
    ):
        downloader = GitHubPackageDownloader(auth_resolver=resolver)
        with patch.object(downloader, "download_raw_file", side_effect=throttle):
            downloader._strategies._git_file_transport_factory = MagicMock(return_value=transport)
            with pytest.raises(RuntimeError, match="sparse Git transport failed") as error:
                downloader.download_virtual_file_package(dep, tmp_path / "package")

    resolve.assert_not_called()
    assert "GitHub API throttle" in str(error.value)
    git_error = error.value.__cause__
    assert isinstance(git_error, GitFileTransportError)
    assert (git_error.__cause__ or git_error.__context__) is cause
    transport.fetch_file_with_commit.assert_called_once_with(dep.virtual_path)


def _assert_github_basic_header(value: str, token: str) -> None:
    """Assert one canonical x-access-token Basic header."""
    assert value.startswith("Authorization: Basic ")
    encoded = value.removeprefix("Authorization: Basic ")
    assert base64.b64decode(encoded).decode() == f"x-access-token:{token}"


def _git_config_entries(env: dict[str, str]) -> dict[str, list[str]]:
    """Return indexed Git config values grouped by normalized key."""
    entries: dict[str, list[str]] = {}
    for index in range(int(env.get("GIT_CONFIG_COUNT", "0"))):
        key = env.get(f"GIT_CONFIG_KEY_{index}", "").lower()
        entries.setdefault(key, []).append(env.get(f"GIT_CONFIG_VALUE_{index}", ""))
    return entries


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


def _dep() -> DependencyReference:
    """Build one GitHub virtual-file dependency."""
    return DependencyReference(
        repo_url="owner/repo",
        host="github.com",
        reference="main",
        virtual_path="instructions/example.instructions.md",
        is_virtual=True,
    )
