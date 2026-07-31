"""Regression contracts for public github.com anonymous-first auth."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from urllib.parse import urlparse

import pytest
import requests

from apm_cli.core.auth import AuthResolver
from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.github_rate_limit import GitHubThrottle, GitHubThrottleError
from apm_cli.deps.transport_selection import (
    NoOpInsteadOfResolver,
    ProtocolPreference,
    TransportSelector,
)
from apm_cli.models.dependency.reference import DependencyReference

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


def _git_auth_entries(env: dict[str, str]) -> list[tuple[str, str]]:
    """Return only config entries that can carry an Authorization header."""
    return [
        (key, value)
        for key, value in _indexed_git_config(env)
        if (value and "extraheader" in key.lower())
        or value.strip().lower().startswith("authorization:")
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


def _assert_anonymous_attempt_env(env: dict[str, str]) -> None:
    """Assert the complete credential-free anonymous Git call shape."""
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == "echo"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    if os.name == "nt":
        assert Path(env["GIT_CONFIG_GLOBAL"]).is_file()
    else:
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "GIT_TOKEN" not in env
    assert "GIT_HTTP_EXTRAHEADER" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert not (_GITHUB_TOKEN_ENV_NAMES & env.keys())
    assert not any(name.startswith("GITHUB_APM_PAT_") for name in env)
    assert _git_auth_entries(env) == []

    config = dict(_indexed_git_config(env))
    assert config["credential.helper"] == ""
    assert config["http.extraheader"] == ""
    assert config["credential.interactive"] == "never"
    assert config["url.file:///fixture.git/.insteadOf"] == ("https://github.com/acme/widgets")
    assert config["http.sslCAInfo"] == "/authorization/corporate-ca.pem"


@pytest.mark.windows_compat
def test_public_github_success_never_resolves_or_leaks_credentials() -> None:
    """A successful public request runs before every credential source."""
    manager = MagicMock(spec=GitHubTokenManager)
    manager.get_token_for_purpose.return_value = "eager-token"
    resolver = AuthResolver(token_manager=manager)
    attempts: list[tuple[str | None, dict[str, str]]] = []

    def operation(token: str | None, env: dict[str, str]) -> str:
        attempts.append((token, env))
        return "public-ok"

    with (
        patch.dict(os.environ, _poisoned_environment(), clear=True),
        patch.object(resolver, "resolve", wraps=resolver.resolve) as resolve,
    ):
        result = resolver.try_with_fallback(
            "github.com",
            operation,
            org="acme",
            path="acme/widgets",
            unauth_first=True,
        )

    assert result == "public-ok"
    assert len(attempts) == 1
    assert attempts[0][0] is None
    _assert_anonymous_attempt_env(attempts[0][1])
    resolve.assert_not_called()
    manager.get_token_for_purpose.assert_not_called()
    manager.resolve_credential_from_gh_cli.assert_not_called()
    manager.resolve_credential_from_git.assert_not_called()


def test_public_github_preserves_caller_git_config_isolation(tmp_path: Path) -> None:
    git_config = tmp_path / "gitconfig"
    git_config.write_text(
        '[url "file:///fixture.git/"]\n\tinsteadOf = https://github.com/acme/widgets\n',
        encoding="ascii",
    )

    env = AuthResolver.build_public_github_anonymous_git_env(
        base_env={
            "GIT_CONFIG_GLOBAL": str(git_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GITHUB_TOKEN": "must-not-leak",
        }
    )

    assert env["GIT_CONFIG_GLOBAL"] == str(git_config)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GITHUB_TOKEN" not in env
    assert dict(_indexed_git_config(env))["credential.helper"] == ""


def test_public_github_ignores_ambient_git_config_isolation(tmp_path: Path) -> None:
    ambient_config = tmp_path / "ambient-gitconfig"
    ambient_config.write_text(
        '[http "https://github.com/"]\n\textraHeader = Authorization: secret\n',
        encoding="ascii",
    )

    with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(ambient_config)}, clear=True):
        env = AuthResolver.build_public_github_anonymous_git_env()

    assert env["GIT_CONFIG_GLOBAL"] != str(ambient_config)
    config = dict(_indexed_git_config(env))
    assert config["credential.helper"] == ""
    assert config["http.extraheader"] == ""


@pytest.mark.parametrize("status_code", (401, 403, 404))
def test_public_github_auth_status_resolves_once_then_retries(status_code: int) -> None:
    """Only an auth-shaped anonymous response unlocks credential resolution."""
    resolver = AuthResolver()
    attempts: list[tuple[str | None, dict[str, str]]] = []

    def operation(token: str | None, env: dict[str, str]) -> str:
        attempts.append((token, env))
        if token is None:
            raise _HttpStatusError(status_code)
        return "private-ok"

    with (
        patch.dict(
            os.environ,
            {**_poisoned_environment(), "GITHUB_APM_PAT": "private-token"},
            clear=True,
        ),
        patch.object(resolver, "resolve", wraps=resolver.resolve) as resolve,
        patch.object(
            GitHubTokenManager,
            "resolve_credential_from_gh_cli",
            side_effect=AssertionError("env token must short-circuit gh"),
        ),
        patch.object(
            GitHubTokenManager,
            "resolve_credential_from_git",
            side_effect=AssertionError("env token must short-circuit credential fill"),
        ),
    ):
        result = resolver.try_with_fallback(
            "github.com",
            operation,
            org="acme",
            path="acme/private",
            unauth_first=True,
        )

    assert result == "private-ok"
    assert [token for token, _env in attempts] == [None, "org-pat-private-fallback"]
    _assert_anonymous_attempt_env(attempts[0][1])
    resolve.assert_called_once_with(
        "github.com",
        "acme",
        port=None,
        host_type=None,
        path="acme/private",
    )


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
    ]


def _public_downloader(resolver: AuthResolver) -> GitHubPackageDownloader:
    """Build a deterministic HTTPS-only downloader for clone-engine tests."""
    return GitHubPackageDownloader(
        auth_resolver=resolver,
        transport_selector=TransportSelector(NoOpInsteadOfResolver()),
        protocol_pref=ProtocolPreference.HTTPS,
        allow_fallback=False,
    )


@pytest.mark.windows_compat
def test_public_github_clone_is_anonymous_before_inherited_pat() -> None:
    """The download transport does not resolve or present an inherited PAT."""
    manager = MagicMock(spec=GitHubTokenManager)
    manager.setup_environment.side_effect = lambda: dict(os.environ)
    manager.get_token_for_purpose.return_value = "eager-token"
    resolver = AuthResolver(token_manager=manager)
    dep_ref = DependencyReference.parse("https://github.com/acme/widgets.git#main")
    calls: list[tuple[str, dict[str, str]]] = []

    def clone_action(url: str, env: dict[str, str], _target: Path) -> None:
        calls.append((url, env))

    with patch.dict(os.environ, _poisoned_environment(), clear=True):
        downloader = _public_downloader(resolver)
        manager.reset_mock()
        downloader._execute_transport_plan(
            dep_ref.repo_url,
            Path("unused-target"),
            dep_ref=dep_ref,
            clone_action=clone_action,
        )

    assert len(calls) == 1
    parsed = urlparse(calls[0][0])
    assert parsed.scheme == "https"
    assert parsed.hostname == "github.com"
    assert parsed.username is None
    assert parsed.password is None
    _assert_anonymous_attempt_env(calls[0][1])
    manager.get_token_for_purpose.assert_not_called()
    manager.resolve_credential_from_gh_cli.assert_not_called()
    manager.resolve_credential_from_git.assert_not_called()


def test_public_github_clone_connectivity_failure_does_not_resolve() -> None:
    """Clone error rendering preserves a network failure without auth lookup."""
    manager = MagicMock(spec=GitHubTokenManager)
    manager.setup_environment.side_effect = lambda: dict(os.environ)
    manager.get_token_for_purpose.return_value = None
    manager.resolve_credential_from_gh_cli.return_value = None
    manager.resolve_credential_from_git.return_value = None
    resolver = AuthResolver(token_manager=manager)
    dep_ref = DependencyReference.parse("https://github.com/acme/widgets.git#main")

    def clone_action(
        _url: str,
        _env: dict[str, str],
        _target: Path,
    ) -> None:
        raise subprocess.CalledProcessError(
            128,
            ("git", "clone"),
            stderr="fatal: could not resolve host: github.com",
        )

    with patch.dict(os.environ, {}, clear=True):
        downloader = _public_downloader(resolver)
        manager.reset_mock()
        with pytest.raises(
            RuntimeError,
            match="network error, not an auth failure",
        ):
            downloader._execute_transport_plan(
                dep_ref.repo_url,
                Path("unused-target"),
                dep_ref=dep_ref,
                clone_action=clone_action,
            )

    manager.get_token_for_purpose.assert_not_called()
    manager.resolve_credential_from_gh_cli.assert_not_called()
    manager.resolve_credential_from_git.assert_not_called()


def test_private_github_clone_resolves_one_path_scoped_fallback() -> None:
    """A private-repo-shaped failure retries through one scoped credential fill."""
    manager = MagicMock(spec=GitHubTokenManager)
    manager.setup_environment.side_effect = lambda: dict(os.environ)
    manager.get_token_for_purpose.return_value = None
    manager.resolve_credential_from_gh_cli.return_value = None
    manager.resolve_credential_from_git.return_value = "private-token"
    resolver = AuthResolver(token_manager=manager)
    dep_ref = DependencyReference.parse("https://github.com/acme/private.git#main")
    calls: list[tuple[str, dict[str, str]]] = []

    def clone_action(url: str, env: dict[str, str], _target: Path) -> None:
        calls.append((url, env))
        if urlparse(url).username is None:
            raise subprocess.CalledProcessError(
                128,
                ("git", "clone"),
                stderr="remote: Repository not found\nfatal: HTTP 404",
            )

    inherited = _poisoned_environment()
    for name in tuple(inherited):
        if name in _GITHUB_TOKEN_ENV_NAMES or name.startswith("GITHUB_APM_PAT_"):
            inherited.pop(name)
    with patch.dict(os.environ, inherited, clear=True):
        downloader = _public_downloader(resolver)
        downloader._execute_transport_plan(
            dep_ref.repo_url,
            Path("unused-target"),
            dep_ref=dep_ref,
            clone_action=clone_action,
        )

    assert len(calls) == 2
    anonymous_url = urlparse(calls[0][0])
    authenticated_url = urlparse(calls[1][0])
    assert anonymous_url.hostname == authenticated_url.hostname == "github.com"
    assert anonymous_url.username is None
    assert authenticated_url.username is not None
    _assert_anonymous_attempt_env(calls[0][1])
    manager.resolve_credential_from_git.assert_called_once_with(
        "github.com",
        port=None,
        path="acme/private",
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
    calls: list[str] = []

    with patch.dict(os.environ, {}, clear=True):
        downloader = _public_downloader(resolver)
        downloader._execute_transport_plan(
            dep_ref.repo_url,
            Path("unused-target"),
            dep_ref=dep_ref,
            clone_action=lambda url, _env, _target: calls.append(url),
        )

    assert len(calls) == 1
    parsed = urlparse(calls[0])
    assert parsed.hostname == "contoso.ghe.com"
    assert parsed.username is not None
    manager.get_token_for_purpose.assert_called()
