"""Regression contracts for public github.com anonymous-first auth."""

from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, call, patch
from urllib.parse import urlparse

import pytest
import requests

from apm_cli.core.auth import _GIT_MESSAGE_LOCALE_ENV, AuthResolver
from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.github_rate_limit import GitHubThrottle, GitHubThrottleError
from apm_cli.deps.transport_selection import (
    NoOpInsteadOfResolver,
    ProtocolPreference,
    TransportSelector,
)
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.unit

_GITHUB_TOKEN_ENV_NAMES = {
    "GH_TOKEN",
    "GITHUB_APM_PAT",
    "GITHUB_TOKEN",
}


class _HttpStatusError(RuntimeError):
    """Minimal HTTP-shaped failure consumed by AuthResolver."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


def _indexed_git_config(env: dict[str, str]) -> list[tuple[str, str]]:
    """Return process-scoped Git config entries in declared order."""
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    return [
        (
            env.get(f"GIT_CONFIG_KEY_{index}", ""),
            env.get(f"GIT_CONFIG_VALUE_{index}", ""),
        )
        for index in range(count)
    ]


def _poisoned_environment() -> dict[str, str]:
    """Return inherited auth state plus harmless Git policy entries."""
    return {
        "GH_TOKEN": "gh-token-must-not-leak",
        "GITHUB_APM_PAT": "apm-pat-must-not-leak",
        "GITHUB_APM_PAT_ACME": "org-pat-private-fallback",
        "GITHUB_TOKEN": "actions-token-must-not-leak",
        "GIT_HTTP_EXTRAHEADER": "Authorization: Bearer inherited-header",
        "GIT_TOKEN": "git-token-must-not-leak",
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic inherited-header",
        "GIT_CONFIG_KEY_1": "url.file:///fixture.git/.insteadOf",
        "GIT_CONFIG_VALUE_1": "https://github.com/acme/widgets",
        "GIT_CONFIG_KEY_2": "credential.interactive",
        "GIT_CONFIG_VALUE_2": "never",
        "GIT_CONFIG_KEY_3": "http.sslCAInfo",
        "GIT_CONFIG_VALUE_3": "/authorization/corporate-ca.pem",
    }


@pytest.mark.parametrize(
    "failure",
    (
        requests.exceptions.ConnectionError("DNS unavailable"),
        requests.exceptions.SSLError("certificate verify failed"),
        requests.exceptions.Timeout("request timed out"),
        _HttpStatusError(429),
        RuntimeError("GitHub secondary rate limit"),
        GitHubThrottleError(
            GitHubThrottle(403, "remaining-zero"),
            "github.com",
        ),
    ),
)
def test_public_github_connectivity_and_throttle_failures_never_resolve(
    failure: Exception,
) -> None:
    """Connectivity and throttle failures surface without an auth prompt."""
    resolver = AuthResolver()

    def operation(_token: str | None, _env: dict[str, str]) -> str:
        raise failure

    with (
        patch.dict(os.environ, _poisoned_environment(), clear=True),
        patch.object(
            resolver,
            "resolve",
            side_effect=AssertionError("non-auth failure must not resolve credentials"),
        ) as resolve,
    ):
        with pytest.raises(type(failure)):
            resolver.try_with_fallback(
                "github.com",
                operation,
                org="acme",
                path="acme/widgets",
                unauth_first=True,
            )

    resolve.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        (_HttpStatusError(401), True),
        (_HttpStatusError(403), True),
        (_HttpStatusError(404), True),
        (RuntimeError("Authentication failed"), True),
        (RuntimeError("Authorization failed"), True),
        (RuntimeError("Unauthorized"), True),
        (RuntimeError("could not read Username"), True),
        (RuntimeError("invalid username or password"), True),
        (RuntimeError("remote: Repository not found"), True),
        (RuntimeError("terminal prompts disabled"), True),
        (RuntimeError("unable to get password from user"), True),
        (RuntimeError("fatal: unable to get password for user"), True),
        (RuntimeError("The requested URL returned error: 404"), True),
        (RuntimeError("HTTP error 403"), True),
        (RuntimeError("status code=401"), True),
        (RuntimeError("GitHub API throttle for github.com (HTTP 403)"), False),
        (RuntimeError("too many requests: HTTP 403"), False),
        (RuntimeError("could not resolve host: github.com"), False),
        (RuntimeError("connection refused"), False),
        (RuntimeError("connection timed out"), False),
        (RuntimeError("network is unreachable"), False),
        (RuntimeError("certificate verify failed"), False),
        (RuntimeError("CERTIFICATE_VERIFY_FAILED"), False),
        (RuntimeError("SSL certificate problem"), False),
        (RuntimeError("TLS verification failed"), False),
    ),
)
def test_public_github_auth_failure_classifier_signal_vocabulary(
    failure: Exception,
    expected: bool,
) -> None:
    """Every documented auth and non-auth signal stays in its intended bucket."""
    assert AuthResolver.is_public_github_auth_failure(failure) is expected


@pytest.mark.parametrize(
    "locale_environment",
    (
        {"LC_ALL": "id_ID.UTF-8", "LANGUAGE": "id_ID:id"},
        {"LANG": "de_DE.UTF-8"},
        {"LC_MESSAGES": "ja_JP.UTF-8"},
    ),
)
def test_git_message_locale_is_normalized_over_inherited_locale(
    locale_environment: dict[str, str],
) -> None:
    """An inherited locale never reaches git, whichever variable carries it."""
    with patch.dict(os.environ, locale_environment, clear=True):
        env = AuthResolver._build_git_env()

    assert env["LC_ALL"] == "C"
    assert env["LANGUAGE"] == "C"


def test_git_message_locale_constant_rejects_mutation() -> None:
    """The pinned locale cannot be reopened by a caller holding the constant."""
    assert isinstance(_GIT_MESSAGE_LOCALE_ENV, MappingProxyType)

    with pytest.raises(TypeError):
        _GIT_MESSAGE_LOCALE_ENV["LC_ALL"] = "id_ID.UTF-8"  # type: ignore[index]

    assert AuthResolver._build_git_env()["LC_ALL"] == "C"


def test_clear_git_auth_env_only_removes_real_auth_channels() -> None:
    """Authorization-like path text survives while real headers are stripped."""
    env = {
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": "http.sslCAInfo",
        "GIT_CONFIG_VALUE_0": "/authorization/corporate-ca.pem",
        "GIT_CONFIG_KEY_1": "custom.policy",
        "GIT_CONFIG_VALUE_1": "X-Custom: authorization=reviewed",
        "GIT_CONFIG_KEY_2": "custom.header",
        "GIT_CONFIG_VALUE_2": "Authorization: Bearer secret",
        "GIT_CONFIG_KEY_3": "http.extraHeader",
        "GIT_CONFIG_VALUE_3": "X-Harmless: value",
    }

    AuthResolver._clear_git_auth_env(env)

    assert _indexed_git_config(env) == [
        ("http.sslCAInfo", "/authorization/corporate-ca.pem"),
        ("custom.policy", "X-Custom: authorization=reviewed"),
        ("http.extraHeader", "X-Harmless: value"),
    ]


def _public_downloader(resolver: AuthResolver) -> GitHubPackageDownloader:
    """Build a deterministic HTTPS-only downloader for clone-engine tests."""
    return GitHubPackageDownloader(
        auth_resolver=resolver,
        transport_selector=TransportSelector(NoOpInsteadOfResolver()),
        protocol_pref=ProtocolPreference.HTTPS,
        allow_fallback=False,
    )


def test_path_scoped_resolution_caches_per_repository() -> None:
    """The same repo reuses auth while another repo gets its own credential."""
    manager = MagicMock(spec=GitHubTokenManager)
    manager.get_token_for_purpose.return_value = None
    manager.resolve_credential_from_gh_cli.return_value = None
    manager.resolve_credential_from_git.side_effect = lambda _host, *, port, path: (
        f"token-for-{path}"
    )
    resolver = AuthResolver(token_manager=manager)

    first = resolver.resolve(
        "github.com",
        "acme",
        path="acme/private-a",
    )
    repeated = resolver.resolve(
        "github.com",
        "acme",
        path="acme/private-a",
    )
    second = resolver.resolve(
        "github.com",
        "acme",
        path="acme/private-b",
    )

    assert repeated is first
    assert first.token == "token-for-acme/private-a"
    assert second.token == "token-for-acme/private-b"
    assert manager.resolve_credential_from_git.call_args_list == [
        call("github.com", port=None, path="acme/private-a"),
        call("github.com", port=None, path="acme/private-b"),
    ]
    assert resolver.has_cached_resolution(
        "github.com",
        "acme",
        path="acme/private-a",
    )
    assert resolver.has_cached_resolution(
        "github.com",
        "acme",
        path="acme/private-b",
    )
    assert not resolver.has_cached_resolution(
        "github.com",
        "acme",
        path="acme/private-c",
    )
    assert not resolver.has_cached_resolution("github.com", "acme")


def test_try_with_fallback_preserves_host_type_conflict_checks() -> None:
    """The inner owner sees the same manifest host hint as its caller."""
    resolver = AuthResolver()
    operation = MagicMock()

    with pytest.raises(
        ValueError,
        match="conflicts with recognized 'github' host",
    ):
        resolver.try_with_fallback(
            "github.com",
            operation,
            host_type="gitlab",
            unauth_first=True,
        )

    operation.assert_not_called()


def _response(status_code: int, content: bytes = b"") -> requests.Response:
    """Build a concrete requests response for file-fetch fallback tests."""
    response = requests.Response()
    response.status_code = status_code
    response._content = content
    response.url = "https://api.github.com/repos/acme/widgets/contents/SKILL.md"
    return response


def test_public_github_file_fetch_sends_no_auth_or_resolver_call() -> None:
    """The Contents API public success remains fully anonymous."""
    manager = MagicMock(spec=GitHubTokenManager)
    manager.setup_environment.side_effect = lambda: dict(os.environ)
    manager.get_token_for_purpose.return_value = "eager-token"
    resolver = AuthResolver(token_manager=manager)
    dep_ref = DependencyReference.parse("https://github.com/acme/widgets.git#main")

    with patch.dict(os.environ, _poisoned_environment(), clear=True):
        downloader = _public_downloader(resolver)
        manager.reset_mock()
        downloader._strategies.try_raw_download = MagicMock(return_value=None)
        downloader._resilient_get = MagicMock(
            return_value=_response(200, b"public-file"),
        )
        content = downloader._strategies.download_github_file(
            dep_ref,
            "SKILL.md",
            "main",
        )

    assert content == b"public-file"
    calls = downloader._resilient_get.call_args_list
    assert len(calls) == 1
    assert "Authorization" not in calls[0].kwargs["headers"]
    manager.get_token_for_purpose.assert_not_called()
    manager.resolve_credential_from_gh_cli.assert_not_called()
    manager.resolve_credential_from_git.assert_not_called()


def test_private_github_file_fetch_uses_one_scoped_fallback() -> None:
    """Anonymous 404s unlock one credential resolution and authenticated retry."""
    manager = MagicMock(spec=GitHubTokenManager)
    manager.setup_environment.side_effect = lambda: dict(os.environ)
    manager.get_token_for_purpose.return_value = None
    manager.resolve_credential_from_gh_cli.return_value = None
    manager.resolve_credential_from_git.return_value = "private-token"
    resolver = AuthResolver(token_manager=manager)
    dep_ref = DependencyReference.parse("https://github.com/acme/private.git#main")
    inherited = _poisoned_environment()
    for name in tuple(inherited):
        if name in _GITHUB_TOKEN_ENV_NAMES or name.startswith("GITHUB_APM_PAT_"):
            inherited.pop(name)

    with patch.dict(os.environ, inherited, clear=True):
        downloader = _public_downloader(resolver)
        downloader._strategies.try_raw_download = MagicMock(return_value=None)
        downloader._resilient_get = MagicMock(
            side_effect=(
                _response(404),
                _response(404),
                _response(200, b"private-file"),
            )
        )
        content = downloader._strategies.download_github_file(
            dep_ref,
            "SKILL.md",
            "main",
        )

    assert content == b"private-file"
    calls = downloader._resilient_get.call_args_list
    assert len(calls) == 3
    assert "Authorization" not in calls[0].kwargs["headers"]
    assert "Authorization" not in calls[1].kwargs["headers"]
    assert calls[2].kwargs["headers"]["Authorization"] == "token private-token"
    manager.resolve_credential_from_git.assert_called_once_with(
        "github.com",
        port=None,
        path="acme/private",
    )


def test_ghe_cloud_https_keeps_existing_auth_first_behavior() -> None:
    """The anonymous-first rule is exact to github.com, not GHE Cloud."""
    manager = MagicMock(spec=GitHubTokenManager)
    manager.setup_environment.side_effect = lambda: dict(os.environ)
    manager.get_token_for_purpose.return_value = "ghe-token"
    resolver = AuthResolver(token_manager=manager)
    dep_ref = DependencyReference.parse("https://contoso.ghe.com/acme/widgets.git#main")
    calls: list[tuple[str, dict[str, str]]] = []

    with patch.dict(os.environ, {}, clear=True):
        downloader = _public_downloader(resolver)
        downloader._execute_transport_plan(
            dep_ref.repo_url,
            Path("unused-target"),
            dep_ref=dep_ref,
            clone_action=lambda url, env, _target: calls.append((url, env)),
        )

    assert len(calls) == 1
    parsed = urlparse(calls[0][0])
    assert parsed.hostname == "contoso.ghe.com"
    assert parsed.username is None
    assert any(
        value.startswith("Authorization: Basic ")
        for key, value in calls[0][1].items()
        if key.startswith("GIT_CONFIG_VALUE_")
    )
    manager.get_token_for_purpose.assert_called()
