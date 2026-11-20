"""Helper credentials are reported back to Git per the git-credential contract (#3765).

https://git-scm.com/docs/git-credential ("TYPICAL USE OF GIT CREDENTIAL"):
after a credential from ``fill`` is used, ``approve`` it when the operation
succeeded and ``reject`` it when it was rejected, feeding the description
``fill`` returned. These tests run the real Azure DevOps chain and the real
``git credential`` command against a logging helper configured in an
isolated global Git config, and assert the exact protocol calls it saw.
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.core import auth_chain
from apm_cli.core.auth import AuthResolver
from apm_cli.core.auth_chain import AdoAuthChainExhaustedError, report_helper_outcome
from apm_cli.core.auth_outcome import AuthAttemptFailed, AuthOutcome, AuthOutcomeKind
from apm_cli.core.token_manager import GitHelperCredential, GitHubTokenManager
from tests.utils.local_ado_org_fixture import (
    LoggingCredentialHelper,
    install_logging_credential_helper,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX helper launcher"),
]

HOST = "dev.azure.com"
REPO_PATH = "contoso/platform/agent-skills"
HELPER_PATH = "contoso/platform/_git/agent-skills"
USER = "helper-user@contoso.example"
SECRET = "helper-secret-sentinel-3765"
PAT = "env-pat-sentinel-3765"
BEARER = "az-bearer-sentinel-3765"


def _response(status: int, headers: dict[str, str] | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response._content = b""
    return response


def _unavailable_bearer() -> MagicMock:
    provider = MagicMock()
    provider.is_available.return_value = False
    provider.last_error = None
    return provider


def _bearer(token: str = BEARER) -> MagicMock:
    provider = MagicMock()
    provider.is_available.return_value = True
    provider.get_bearer_token.return_value = token
    provider.last_error = None
    return provider


@pytest.fixture
def helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LoggingCredentialHelper:
    """An isolated HOME and global Git config with only the logging helper."""
    for name in list(os.environ):
        if name.startswith(("GIT_", "GCM_", "ADO_", "GITHUB_", "GH_", "GITLAB_", "APM_")):
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    git_config = tmp_path / "gitconfig"
    git_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("CI", "1")
    auth_chain._HELPER_REPORTS.clear()
    yield install_logging_credential_helper(
        git_config, tmp_path / "helper", username=USER, password=SECRET, hosts=(HOST,)
    )
    auth_chain._HELPER_REPORTS.clear()


def _actions(helper: LoggingCredentialHelper, action: str) -> list[dict[str, object]]:
    return [event for event in helper.events() if event["action"] == action]


def _run(
    operation,
    *,
    bearer: MagicMock | None = None,
    resolver: AuthResolver | None = None,
    host: str = HOST,
    port: int | None = None,
    path: str = REPO_PATH,
):
    resolver = resolver or AuthResolver()
    with patch(
        "apm_cli.core.azure_cli.get_bearer_provider",
        return_value=bearer or _unavailable_bearer(),
    ):
        return resolver.try_with_fallback(host, operation, org="contoso", port=port, path=path)


def _accept_secret(token, _env):
    """Anonymous gets the sign-in redirect; only the helper secret is accepted."""
    if token == SECRET:
        return "ok"
    raise AuthAttemptFailed(response=_response(401 if token else 203))


def _helper_answers(helper_outcome):
    """Anonymous is refused; the helper attempt sees *helper_outcome*."""

    def _operation(token, _env):
        if token != SECRET:
            raise AuthAttemptFailed(response=_response(401 if token else 203))
        if isinstance(helper_outcome, BaseException):
            raise helper_outcome
        raise AuthAttemptFailed(response=helper_outcome)

    return _operation


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


def test_accepted_helper_credential_is_approved_once_with_fill_description(helper):
    resolver = AuthResolver()
    assert _run(_accept_secret, resolver=resolver) == "ok"
    assert _run(_accept_secret, resolver=resolver) == "ok"

    gets = _actions(helper, "get")
    stores = _actions(helper, "store")
    assert len(gets) == 1, helper.events()
    assert len(stores) == 1, helper.events()
    assert _actions(helper, "erase") == []
    fields = stores[0]["fields"]
    assert fields["protocol"] == "https"
    assert fields["host"] == HOST
    assert fields["path"] == HELPER_PATH
    assert fields["username"] == USER
    assert fields["password"] == SECRET
    assert stores[0]["env"]["GCM_INTERACTIVE"] == "0"
    assert stores[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_concurrent_workers_report_once_per_key(helper):
    resolver = AuthResolver()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _run(_accept_secret, resolver=resolver), range(8)))
    assert results == ["ok"] * 8
    assert len(_actions(helper, "store")) == 1


def test_returned_success_response_is_approved(helper):
    def _operation(token, _env):
        if token != SECRET:
            raise AuthAttemptFailed(response=_response(401 if token else 203))
        return _response(200, {"Content-Type": "application/octet-stream"})

    assert _run(_operation).status_code == 200
    assert len(_actions(helper, "store")) == 1


@pytest.mark.parametrize(
    "returned",
    [
        pytest.param(_response(404), id="404"),
        pytest.param(_response(500), id="500"),
        pytest.param(_response(302, {"Location": "https://elsewhere.example/r"}), id="302"),
    ],
)
def test_returned_non_success_response_is_not_approved(helper, returned):
    def _operation(token, _env):
        if token != SECRET:
            raise AuthAttemptFailed(response=_response(401 if token else 203))
        return returned

    assert _run(_operation) is returned
    assert _actions(helper, "store") == []
    assert _actions(helper, "erase") == []


def test_secret_accepted_on_this_host_is_not_rejected_for_another_path(helper):
    resolver = AuthResolver()
    assert _run(_accept_secret, resolver=resolver) == "ok"
    with pytest.raises(AdoAuthChainExhaustedError):
        _run(_helper_answers(_response(401)), resolver=resolver, path="other/project/repo")
    assert _actions(helper, "erase") == [], helper.events()


def test_host_scoped_helper_without_use_http_path(tmp_path, helper):
    git_config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    git_config.write_text("", encoding="utf-8")
    plain = install_logging_credential_helper(
        git_config,
        tmp_path / "plain-helper",
        username=USER,
        password=SECRET,
        hosts=(HOST,),
        use_http_path=False,
    )
    assert _run(_accept_secret) == "ok"
    stores = _actions(plain, "store")
    assert len(stores) == 1, plain.events()
    assert "path" not in stores[0]["fields"]
    assert stores[0]["fields"]["host"] == HOST


def test_ado_server_host_with_port_reports_port_in_host_field(tmp_path, helper, monkeypatch):
    server = "ado.example.com"
    monkeypatch.setenv("ADO_HOST", server)
    git_config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    git_config.write_text("", encoding="utf-8")
    logged = install_logging_credential_helper(
        git_config,
        tmp_path / "server-helper",
        username=USER,
        password=SECRET,
        hosts=(f"{server}:8443",),
    )
    assert _run(_accept_secret, host=server, port=8443) == "ok"
    stores = _actions(logged, "store")
    assert len(stores) == 1, logged.events()
    assert stores[0]["fields"]["host"] == f"{server}:8443"


def test_approve_is_once_per_process_across_resolvers(helper):
    assert _run(_accept_secret) == "ok"
    assert _run(_accept_secret) == "ok"
    assert len(_actions(helper, "get")) == 2
    assert len(_actions(helper, "store")) == 1


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "helper_outcome",
    [
        pytest.param(_response(401), id="http-401"),
        pytest.param(
            RuntimeError(
                "fatal: could not read Username for 'https://dev.azure.com': "
                "terminal prompts disabled"
            ),
            id="git-401-prompt",
        ),
        pytest.param(
            RuntimeError("fatal: Authentication failed for 'https://dev.azure.com/contoso/'"),
            id="git-authentication-failed",
        ),
    ],
)
def test_verified_rejection_rejects_once_and_drops_the_cache(helper, helper_outcome):
    resolver = AuthResolver()
    for _ in range(3):
        with pytest.raises(AdoAuthChainExhaustedError):
            _run(_helper_answers(helper_outcome), resolver=resolver)

    erases = _actions(helper, "erase")
    assert len(erases) == 1, helper.events()
    assert _actions(helper, "store") == []
    fields = erases[0]["fields"]
    assert (fields["host"], fields["path"], fields["username"], fields["password"]) == (
        HOST,
        HELPER_PATH,
        USER,
        SECRET,
    )
    # Every rejected answer was dropped, so each operation asked fill again.
    assert len(_actions(helper, "get")) == 3


def test_rejected_then_refreshed_credential_is_approved(helper):
    resolver = AuthResolver()
    with pytest.raises(AdoAuthChainExhaustedError):
        _run(_helper_answers(_response(401)), resolver=resolver)
    assert _run(_accept_secret, resolver=resolver) == "ok"
    assert [event["action"] for event in helper.events()] == ["get", "erase", "get", "store"]


# ---------------------------------------------------------------------------
# no report for ambiguous outcomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("helper_outcome", "exhausted"),
    [
        pytest.param(_response(403), True, id="http-403"),
        pytest.param(_response(203, {"Content-Type": "text/html"}), True, id="signin-page"),
        pytest.param(
            _response(302, {"Location": "https://app.vssps.visualstudio.com/_signin"}),
            True,
            id="signin-redirect",
        ),
        pytest.param(
            RuntimeError("fatal: unable to access: The requested URL returned error: 403"),
            True,
            id="git-403",
        ),
        pytest.param(_response(404), False, id="http-404"),
        pytest.param(_response(500), False, id="http-500"),
        pytest.param(_response(429), False, id="http-429"),
        pytest.param(
            _response(302, {"Location": "https://elsewhere.example/repo"}),
            False,
            id="off-origin-redirect",
        ),
        pytest.param(requests.exceptions.Timeout("read timed out"), False, id="timeout"),
        pytest.param(requests.exceptions.SSLError("certificate verify failed"), False, id="tls"),
        pytest.param(
            RuntimeError("fatal: unable to access: Could not resolve host: dev.azure.com"),
            False,
            id="network",
        ),
    ],
)
def test_ambiguous_helper_outcome_is_not_reported(helper, helper_outcome, exhausted):
    expected = AdoAuthChainExhaustedError if exhausted else Exception
    with pytest.raises(expected):
        _run(_helper_answers(helper_outcome))
    assert len(_actions(helper, "get")) == 1
    assert _actions(helper, "store") == []
    assert _actions(helper, "erase") == []


# ---------------------------------------------------------------------------
# env PAT, az bearer, and non-ADO credentials are never reported
# ---------------------------------------------------------------------------


def test_accepted_env_pat_is_never_reported(helper, monkeypatch):
    monkeypatch.setenv("ADO_APM_PAT", PAT)
    assert _run(lambda token, _env: "ok" if token == PAT else None) == "ok"
    assert helper.events() == ()


def test_rejected_pat_and_bearer_are_never_reported(helper, monkeypatch):
    monkeypatch.setenv("ADO_APM_PAT", PAT)
    assert _run(_accept_secret, bearer=_bearer()) == "ok"
    erases = _actions(helper, "erase")
    stores = _actions(helper, "store")
    assert erases == [], helper.events()
    assert [event["fields"]["password"] for event in stores] == [SECRET]


def test_accepted_bearer_is_never_reported(helper):
    assert _run(lambda token, _env: "ok" if token == BEARER else None, bearer=_bearer()) == "ok"
    assert helper.events() == ()


def test_non_ado_helper_credential_is_never_reported(tmp_path, helper, monkeypatch):
    github = install_logging_credential_helper(
        Path(os.environ["GIT_CONFIG_GLOBAL"]),
        tmp_path / "github-helper",
        username=USER,
        password=SECRET,
        hosts=("github.com",),
    )
    monkeypatch.setenv("GITHUB_APM_PAT", PAT)

    def _operation(token, _env):
        if token != SECRET:
            raise RuntimeError("fatal: Authentication failed (HTTP 401)")
        return token

    with patch.object(GitHubTokenManager, "resolve_credential_from_gh_cli", return_value=None):
        assert AuthResolver().try_with_fallback("github.com", _operation) == SECRET
    assert [
        event["action"] for event in github.events() if event["fields"].get("host") == "github.com"
    ] == ["get"]


# ---------------------------------------------------------------------------
# host scoping and fail-safe behavior
# ---------------------------------------------------------------------------


def test_report_targets_only_the_derived_host(helper):
    assert _run(_accept_secret) == "ok"
    for event in helper.events():
        assert event["fields"]["host"] == HOST, event
        assert event["fields"]["path"] == HELPER_PATH, event


def test_credential_for_another_host_is_never_reported(helper):
    filled = GitHubTokenManager.resolve_git_helper_credential(HOST, path=HELPER_PATH)
    assert filled is not None
    resolver = AuthResolver()
    other = AuthResolver.classify_host("myorg.visualstudio.com")
    report_helper_outcome(resolver, other, HELPER_PATH, filled, None)
    reject = AuthOutcome(AuthOutcomeKind.CREDENTIAL_REJECTED, "HTTP 401", 401, True)
    report_helper_outcome(resolver, other, HELPER_PATH, filled, reject)
    assert [event["action"] for event in helper.events()] == ["get"]


def test_credential_not_obtained_from_fill_is_never_reported(helper):
    synthetic = GitHelperCredential(host=HOST, password=SECRET, username=USER, path=HELPER_PATH)
    with patch.object(GitHubTokenManager, "resolve_git_helper_credential", return_value=synthetic):
        assert _run(_accept_secret) == "ok"
    assert helper.events() == ()


def test_hanging_helper_store_never_fails_the_operation(helper, monkeypatch):
    git_config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    with git_config.open("a", encoding="utf-8") as handle:
        handle.write('[credential]\n\thelper = "!f() { test \\"$1\\" = store && sleep 5; }; f"\n')
    monkeypatch.setenv("APM_GIT_CREDENTIAL_TIMEOUT", "1")
    assert _run(_accept_secret) == "ok"
    assert len(_actions(helper, "store")) == 1


def test_report_error_never_fails_the_operation(helper):
    with patch.object(
        GitHubTokenManager, "report_git_helper_credential", side_effect=RuntimeError("boom")
    ):
        assert _run(_accept_secret) == "ok"


def test_secret_accepted_by_another_resolver_is_not_rejected(helper):
    assert _run(_accept_secret, resolver=AuthResolver()) == "ok"
    with pytest.raises(AdoAuthChainExhaustedError):
        _run(_helper_answers(_response(401)), resolver=AuthResolver(), path="other/project/repo")
    assert _actions(helper, "erase") == [], helper.events()


@pytest.mark.parametrize(
    "field",
    [
        pytest.param(("username", "someone-else"), id="username"),
        pytest.param(("path", "other/project/_git/repo"), id="path"),
        pytest.param(("password", "different-secret"), id="password"),
    ],
)
def test_description_not_matching_the_sent_credential_is_never_reported(helper, field):
    filled = GitHubTokenManager.resolve_git_helper_credential(HOST, path=HELPER_PATH)
    assert filled is not None
    changed = tuple(
        (key, field[1] if key == field[0] else value) for key, value in filled.description
    )
    tampered = GitHelperCredential(
        host=filled.host,
        password=filled.password,
        username=filled.username,
        path=filled.path,
        description=changed,
    )
    assert not GitHubTokenManager.report_git_helper_credential(tampered, accepted=True)
    assert _actions(helper, "store") == []


def test_helper_stderr_is_never_logged(helper, caplog):
    git_config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    with git_config.open("a", encoding="utf-8") as handle:
        handle.write(
            '[credential]\n\thelper = "!f() { test \\"$1\\" = store && '
            'echo could not store helper-secret-sentinel-3765 >&2 && exit 1; }; f"\n'
        )
    filled = GitHubTokenManager.resolve_git_helper_credential(HOST, path=HELPER_PATH)
    assert filled is not None
    with caplog.at_level("DEBUG", logger="apm_cli.core.token_manager"):
        GitHubTokenManager.report_git_helper_credential(filled, accepted=True)
    assert SECRET not in caplog.text
