"""Hermetic regressions for the Azure DevOps auth chain (#3752, #3758, #3753).

Every test drives the real AuthResolver and call-site code. HTTP is answered
by a fake ``HTTPAdapter.send`` and ``git credential fill`` by a fake
``subprocess.run`` in the token manager, so no network or real helper is used.
"""

from __future__ import annotations

import base64
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

import apm_cli.core.azure_cli as azure_cli_module
import apm_cli.core.token_manager as token_manager_module
from apm_cli.core.auth import AuthResolver, _redact_secrets
from apm_cli.core.azure_cli import AzureCliBearerError
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.component

HELPER_USER = "alice@contoso.example"
HELPER_SECRET = "helper-secret-value-0123456789"
POLICY_YAML = "name: org-policy\nversion: '1.0'\n"
SIGNIN_LOCATION = "https://app.vssps.visualstudio.com/_signin?realm=dev.azure.com"


def _basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


HELPER_AUTHORIZATION = _basic(HELPER_USER, HELPER_SECRET)


class _FakeProvider:
    """Stand-in for the az CLI bearer provider."""

    def __init__(self, *, available: bool, error: AzureCliBearerError | None = None) -> None:
        self._available = available
        self._error = error
        self.tenant_calls = 0

    def is_available(self) -> bool:
        return self._available

    def get_bearer_token(self) -> str:
        if self._error is not None:
            raise self._error
        raise AzureCliBearerError("not signed in", kind="not_logged_in")

    def get_current_tenant_id(self) -> str | None:
        self.tenant_calls += 1
        return "tenant-0000"

    @property
    def last_error(self) -> AzureCliBearerError | None:
        return self._error


class _FillRecorder:
    """Fake ``git credential fill`` that returns a username and password."""

    def __init__(self, *, username: str | None = HELPER_USER, password: str | None = HELPER_SECRET):
        self.requests: list[str] = []
        self.envs: list[dict[str, str]] = []
        self._username = username
        self._password = password
        self._lock = threading.Lock()
        self._real_run = subprocess.run

    def __call__(self, args, *pargs, **kwargs):
        if list(args)[-2:] != ["credential", "fill"]:
            return self._real_run(args, *pargs, **kwargs)
        with self._lock:
            self.requests.append(kwargs.get("input", ""))
            self.envs.append(dict(kwargs.get("env") or {}))
        if self._password is None:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no credential")
        lines = ["protocol=https", "host=dev.azure.com"]
        if self._username is not None:
            lines.append(f"username={self._username}")
        lines.append(f"password={self._password}")
        return subprocess.CompletedProcess(args, 0, stdout="\n".join(lines) + "\n", stderr="")

    def paths(self) -> list[str]:
        found = []
        for body in self.requests:
            for line in body.splitlines():
                if line.startswith("path="):
                    found.append(line[len("path=") :])
        return found


def _response(
    request: requests.PreparedRequest,
    status: int,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = body
    response.headers = CaseInsensitiveDict(headers or {})
    response.url = request.url
    response.request = request
    response.encoding = "utf-8"
    return response


@pytest.fixture
def ado_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """No PAT, no usable az, isolated HOME, and a recording credential helper."""
    for name in ("ADO_APM_PAT", "ADO_HOST", "APM_ADO_HOSTS", "CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NETRC", str(tmp_path / ".netrc-absent"))
    provider = _FakeProvider(available=False)
    monkeypatch.setattr(azure_cli_module, "get_bearer_provider", lambda: provider)
    fill = _FillRecorder()
    monkeypatch.setattr(token_manager_module.subprocess, "run", fill)
    return provider, fill


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[requests.PreparedRequest], requests.Response],
) -> list[requests.PreparedRequest]:
    seen: list[requests.PreparedRequest] = []

    def _send(_adapter, request, **_kwargs):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(HTTPAdapter, "send", _send)
    return seen


def test_policy_signin_redirect_escalates_to_path_scoped_helper(
    ado_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3758: a 302 to the ADO sign-in page is an auth failure, not a policy error."""
    from apm_cli.policy.discovery import _fetch_ado_contents

    _provider, fill = ado_environment

    def _handler(request: requests.PreparedRequest) -> requests.Response:
        if request.headers.get("Authorization") == HELPER_AUTHORIZATION:
            return _response(request, 200, POLICY_YAML.encode(), {"Content-Type": "text/plain"})
        return _response(request, 302, headers={"Location": SIGNIN_LOCATION})

    seen = _install_http(monkeypatch, _handler)

    content, error = _fetch_ado_contents("contoso", "apm", "apm-policy", "apm-policy.yml")

    assert error is None
    assert content == POLICY_YAML
    assert fill.paths() == ["contoso/apm/_git/apm-policy"]
    assert {urlparse(request.url).hostname for request in seen} == {"dev.azure.com"}


def test_download_signin_page_escalates_to_helper_basic_with_username(
    ado_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3752: an HTML sign-in page from the Items API unlocks the helper step."""
    from apm_cli.deps.github_downloader import GitHubPackageDownloader

    _provider, fill = ado_environment

    def _handler(request: requests.PreparedRequest) -> requests.Response:
        if request.headers.get("Authorization") == HELPER_AUTHORIZATION:
            return _response(request, 200, b"# skill\n", {"Content-Type": "text/plain"})
        return _response(request, 200, b"<html>sign in</html>", {"Content-Type": "text/html"})

    _install_http(monkeypatch, _handler)
    downloader = GitHubPackageDownloader(auth_resolver=AuthResolver())
    dep_ref = DependencyReference.parse("dev.azure.com/contoso/proj/repo")

    content = downloader.download_raw_file(dep_ref, "SKILL.md", "main")

    assert content == b"# skill\n"
    assert fill.paths() == ["contoso/proj/_git/repo"]


def test_download_never_follows_the_signin_redirect(
    ado_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sign-in redirect itself is the auth signal; its target is never contacted."""
    from apm_cli.deps.github_downloader import GitHubPackageDownloader

    _provider, fill = ado_environment

    def _handler(request: requests.PreparedRequest) -> requests.Response:
        if urlparse(request.url).hostname != "dev.azure.com":
            raise requests.exceptions.ConnectionError("sign-in host must not be contacted")
        if request.headers.get("Authorization") == HELPER_AUTHORIZATION:
            return _response(request, 200, b"# skill\n", {"Content-Type": "text/plain"})
        return _response(request, 302, headers={"Location": SIGNIN_LOCATION})

    seen = _install_http(monkeypatch, _handler)
    downloader = GitHubPackageDownloader(auth_resolver=AuthResolver())
    dep_ref = DependencyReference.parse("dev.azure.com/contoso/proj/repo")

    assert downloader.download_raw_file(dep_ref, "SKILL.md", "main") == b"# skill\n"
    assert {urlparse(request.url).hostname for request in seen} == {"dev.azure.com"}
    assert fill.paths() == ["contoso/proj/_git/repo"]


def test_download_ignores_ambient_netrc_for_ado(
    ado_environment, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Azure DevOps requests use only the credential chosen by the auth chain."""
    from apm_cli.deps.github_downloader import GitHubPackageDownloader

    netrc = tmp_path / ".netrc"
    netrc.write_text(
        "machine dev.azure.com login other-user password other-password\n", encoding="ascii"
    )
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("ADO_APM_PAT", "pat-from-environment")
    expected = _basic("", "pat-from-environment")
    authorizations: list[str | None] = []

    def _handler(request: requests.PreparedRequest) -> requests.Response:
        authorizations.append(request.headers.get("Authorization"))
        return _response(request, 200, b"content", {"Content-Type": "text/plain"})

    _install_http(monkeypatch, _handler)
    downloader = GitHubPackageDownloader(auth_resolver=AuthResolver())
    dep_ref = DependencyReference.parse("dev.azure.com/contoso/proj/repo")

    assert downloader.download_raw_file(dep_ref, "SKILL.md", "main") == b"content"
    assert authorizations == [expected]


def test_validation_ls_remote_escalates_to_helper(
    ado_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3752: tokenless ADO validation reaches the path-scoped helper step."""
    import apm_cli.utils.git_env as git_env_module
    from apm_cli.install.validation import _validate_ado_git_package

    _provider, fill = ado_environment
    seen_headers: list[str | None] = []

    def _fake_remote_refs(url, *patterns, env=None, options=(), timeout=None, **_kw):
        header = None
        count = int((env or {}).get("GIT_CONFIG_COUNT", "0") or "0")
        for index in range(count):
            if env.get(f"GIT_CONFIG_KEY_{index}", "").lower() == "http.extraheader":
                header = env.get(f"GIT_CONFIG_VALUE_{index}") or None
        seen_headers.append(header)
        if header == f"Authorization: {HELPER_AUTHORIZATION}":
            return subprocess.CompletedProcess(["git"], 0, stdout="abc\trefs/heads/main\n")
        return subprocess.CompletedProcess(
            ["git"],
            128,
            stdout="",
            stderr="fatal: could not read Username for 'https://dev.azure.com': "
            "terminal prompts disabled\n",
        )

    monkeypatch.setattr(git_env_module, "git_remote_refs", _fake_remote_refs)
    dep_ref = DependencyReference.parse("dev.azure.com/contoso/proj/repo")

    assert _validate_ado_git_package(dep_ref, AuthResolver(), None, "pkg", None) is True
    assert fill.paths() == ["contoso/proj/_git/repo"]
    assert seen_headers[-1] == f"Authorization: {HELPER_AUTHORIZATION}"


def test_helper_fill_runs_once_per_repository_under_concurrency(ado_environment) -> None:
    """Parallel install workers share one path-scoped helper answer."""
    _provider, fill = ado_environment
    resolver = AuthResolver()
    barrier = threading.Barrier(8)
    results: list[str] = []
    lock = threading.Lock()

    def _operation(token, _env):
        if token != HELPER_SECRET:
            raise RuntimeError("HTTP 401 Unauthorized")
        return "ok"

    def _worker() -> None:
        barrier.wait()
        value = resolver.try_with_fallback(
            "dev.azure.com",
            _operation,
            org="contoso",
            path="contoso/proj/_git/repo",
        )
        with lock:
            results.append(value)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert results == ["ok"] * 8
    assert len(fill.requests) == 1


def test_credential_fill_disables_gcm_interaction_in_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-interactive run must never let GCM open a browser or dialog."""
    fill = _FillRecorder()
    monkeypatch.setattr(token_manager_module.subprocess, "run", fill)
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("GCM_INTERACTIVE", raising=False)

    token_manager_module.GitHubTokenManager.resolve_credential_from_git(
        "dev.azure.com", path="contoso/proj/_git/repo"
    )

    assert fill.envs[-1].get("GCM_INTERACTIVE") == "0"


def test_error_context_reports_az_failure_reason_not_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3753: an az token failure is not presented as a rejected bearer token."""
    monkeypatch.delenv("ADO_APM_PAT", raising=False)
    error = AzureCliBearerError(
        "az CLI returned exit code 1",
        kind="not_logged_in",
        stderr="ERROR: AADSTS50076: Due to a configuration change you must use MFA.",
    )
    provider = _FakeProvider(available=True, error=error)
    monkeypatch.setattr(azure_cli_module, "get_bearer_provider", lambda: provider)
    resolver = AuthResolver()

    message = resolver.build_error_context("dev.azure.com", "install", org="contoso")

    assert "AADSTS50076" in message
    assert "returned a bearer token" not in message
    assert "rejected it (HTTP 401)" not in message


def test_secret_redaction_keeps_bearer_token_phrase() -> None:
    """#3753: diagnostics may say 'bearer token' without redacting the words."""
    assert _redact_secrets("ADO bearer token acquisition failed") == (
        "ADO bearer token acquisition failed"
    )
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJmaXh0dXJlIn0.c2lnbmF0dXJlLXZhbHVl"
    assert jwt not in _redact_secrets(f"Authorization: Bearer {jwt}")


def _git_config(env: dict[str, str]) -> list[tuple[str, str]]:
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)]


@pytest.mark.parametrize("pat", ["stale-pat", None], ids=["pat-bearer-helper", "tokenless"])
def test_each_ado_step_child_env_carries_only_its_own_credential(monkeypatch, pat) -> None:
    """Each attempt's git env holds exactly its own header and no helper lookup."""
    for name in ("ADO_APM_PAT", "GITHUB_TOKEN", "GITHUB_APM_PAT", "GH_TOKEN", "GIT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    if pat:
        monkeypatch.setenv("ADO_APM_PAT", pat)
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-token")

    class _Bearer:
        last_error = None

        def is_available(self) -> bool:
            return True

        def get_bearer_token(self) -> str:
            return "az-jwt"

    monkeypatch.setattr(azure_cli_module, "get_bearer_provider", lambda: _Bearer())
    helper = token_manager_module.GitHelperCredential(
        host="dev.azure.com", password="helper-pw", username="me"
    )
    monkeypatch.setattr(
        token_manager_module.GitHubTokenManager,
        "resolve_git_helper_credential",
        staticmethod(lambda *_a, **_k: helper),
    )
    seen: list[tuple[str | None, dict[str, str]]] = []

    def _rejected(token, env):
        seen.append((token, dict(env)))
        raise RuntimeError("fatal: Authentication failed (401)")

    from apm_cli.core.auth_chain import AdoAuthChainExhaustedError

    with pytest.raises(AdoAuthChainExhaustedError, match="Tried in order"):
        AuthResolver().try_with_fallback(
            "dev.azure.com",
            _rejected,
            org="o",
            path="o/p/_git/r",
            remote_url="https://dev.azure.com/o/p/_git/r",
        )

    expected = {
        "stale-pat": "Basic " + base64.b64encode(b":stale-pat").decode(),
        "az-jwt": "Bearer az-jwt",
        "helper-pw": "Basic " + base64.b64encode(b"me:helper-pw").decode(),
    }
    tokens = [token for token, _env in seen]
    assert tokens == (["stale-pat"] if pat else []) + ["az-jwt", "helper-pw"], tokens
    for token, env in seen:
        config = _git_config(env)
        assert ("credential.helper", "") in config
        headers = [value for key, value in config if key == "http.extraheader" and value]
        assert headers == [f"Authorization: {expected[token]}"]
        for name in ("GIT_TOKEN", "ADO_APM_PAT", "GITHUB_TOKEN", "GITHUB_APM_PAT", "GH_TOKEN"):
            assert name not in env
        others = [cred for cred in ("stale-pat", "az-jwt", "helper-pw") if cred != token]
        text = repr(env)
        assert not any(cred in text for cred in others), (token, others)
