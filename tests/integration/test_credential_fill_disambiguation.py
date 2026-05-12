"""Integration test: per-URL credential disambiguation reaches the credential-fill stdin.

Exercises the full validation -> AuthResolver -> token_manager pipeline with the
network and gh-CLI calls stubbed.  The contract under test:

* When a primary token (env var) is rejected by the GitHub API and gh CLI
  returns no token, ``git credential fill`` MUST be invoked with
  ``path=<org/repo>`` so Git Credential Manager (with
  ``credential.useHttpPath = true``) can pick the per-URL account and avoid
  the multi-account picker prompt that motivated PR #630.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.core.token_manager import GitHubTokenManager


@pytest.fixture
def isolated_env():
    """Run with only GITHUB_APM_PAT=bad in the environment."""
    with patch.dict(os.environ, {"GITHUB_APM_PAT": "bad-token"}, clear=True):
        yield


@pytest.fixture
def stub_gh_cli_unavailable():
    """gh CLI returns no token (simulating a user without gh login)."""
    with patch.object(GitHubTokenManager, "resolve_credential_from_gh_cli", return_value=None):
        yield


def _make_credential_fill_stub(captured_stdin: list[str]):
    """Build a subprocess.run stub that records credential-fill stdin and returns a good token."""

    def _stub(*args, **kwargs):
        # Record what APM sent to ``git credential fill``.  This is the
        # contract surface: GCM's per-URL config matches against the path.
        captured_stdin.append(kwargs.get("input", ""))
        return MagicMock(
            returncode=0,
            stdout="protocol=https\nhost=github.com\nusername=u\npassword=good-token\n",
        )

    return _stub


def test_credential_fill_receives_path_for_per_url_disambiguation(
    isolated_env, stub_gh_cli_unavailable
):
    """E2E: validation -> AuthResolver -> credential fill stdin contains path=org/repo."""
    from apm_cli.install.validation import _validate_package_exists

    captured_stdin: list[str] = []
    api_calls: list[str] = []

    def fake_requests_get(url, *args, **kwargs):
        api_calls.append(url)
        headers = kwargs.get("headers", {}) or {}
        auth = headers.get("Authorization", "")
        resp = MagicMock()
        resp.headers = {}
        if "good-token" in auth:
            resp.ok = True
            resp.status_code = 200
        elif "bad-token" in auth:
            # 401 simulates a wrong-account token
            resp.ok = False
            resp.status_code = 401
            resp.reason = "Unauthorized"
        else:
            # Unauthenticated -- 404 (private repo behaviour)
            resp.ok = False
            resp.status_code = 404
            resp.reason = "Not Found"
        return resp

    with (
        patch(
            "subprocess.run", side_effect=_make_credential_fill_stub(captured_stdin)
        ) as mock_subproc,
        patch("apm_cli.install.validation.requests.get", side_effect=fake_requests_get),
    ):
        result = _validate_package_exists("acme/widgets", verbose=False)

    assert result is True, "validation must succeed once the good token is fetched"

    # The recovered (good) token must have been used in a GitHub API call.
    parsed_calls = [urlparse(url) for url in api_calls]
    assert any(
        p.hostname == "api.github.com" and p.path == "/repos/acme/widgets" for p in parsed_calls
    ), f"GitHub API was not called with the right repo: {api_calls!r}"

    # `git credential fill` was invoked at least once, and the stdin includes path=acme/widgets.
    assert captured_stdin, "git credential fill was never invoked"
    last_stdin = captured_stdin[-1]
    assert "path=acme/widgets" in last_stdin, (
        f"credential fill stdin missing path= for per-URL disambiguation; got: {last_stdin!r}"
    )

    # Sanity: subprocess.run was called for `git credential fill` (not just env probes).
    cmd_calls = [
        call.args[0] if call.args else call.kwargs.get("args")
        for call in mock_subproc.call_args_list
    ]
    assert any(
        cmd and "credential" in " ".join(cmd) and "fill" in " ".join(cmd)
        for cmd in cmd_calls
        if isinstance(cmd, (list, tuple))
    ), f"git credential fill not in subprocess calls: {cmd_calls!r}"


def test_gh_cli_success_short_circuits_credential_fill_in_validation(isolated_env):
    """E2E regression trap: when gh CLI returns a token, credential fill must NOT run."""
    from apm_cli.install.validation import _validate_package_exists

    api_calls: list[str] = []

    def fake_requests_get(url, *args, **kwargs):
        api_calls.append(url)
        headers = kwargs.get("headers", {}) or {}
        auth = headers.get("Authorization", "")
        resp = MagicMock()
        resp.headers = {}
        if "gho_from_gh_cli" in auth:
            resp.ok = True
            resp.status_code = 200
        elif "bad-token" in auth:
            resp.ok = False
            resp.status_code = 401
            resp.reason = "Unauthorized"
        else:
            resp.ok = False
            resp.status_code = 404
            resp.reason = "Not Found"
        return resp

    # Patch credential fill so we can assert it is NEVER called.
    with (
        patch.object(
            GitHubTokenManager,
            "resolve_credential_from_gh_cli",
            return_value="gho_from_gh_cli",
        ),
        patch.object(GitHubTokenManager, "resolve_credential_from_git") as mock_cred_fill,
        # subprocess.run still needs a stub in case anything else in the path probes it
        patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout=""),
        ),
        patch("apm_cli.install.validation.requests.get", side_effect=fake_requests_get),
    ):
        result = _validate_package_exists("acme/widgets", verbose=False)

    assert result is True
    mock_cred_fill.assert_not_called()


def test_credential_fill_receives_path_on_parse_failure_fallback(
    isolated_env, stub_gh_cli_unavailable
):
    """E2E (validation.py:590): when DependencyReference.parse raises, the fallback
    branch (`_check_repo_fallback`) must still forward `path=owner/repo` to git
    credential fill. This is a secure-by-default surface -- the parse-failure
    code path threads the same per-URL disambiguation as the happy path.
    """
    from apm_cli.install.validation import _validate_package_exists

    captured_stdin: list[str] = []
    api_calls: list[str] = []

    def fake_requests_get(url, *args, **kwargs):
        api_calls.append(url)
        headers = kwargs.get("headers", {}) or {}
        auth = headers.get("Authorization", "")
        resp = MagicMock()
        resp.headers = {}
        if "good-token" in auth:
            resp.ok = True
            resp.status_code = 200
        elif "bad-token" in auth:
            resp.ok = False
            resp.status_code = 401
            resp.reason = "Unauthorized"
        else:
            resp.ok = False
            resp.status_code = 404
            resp.reason = "Not Found"
        return resp

    # Force the primary parse path to raise so execution falls through to
    # `_check_repo_fallback` (validation.py:585+). Anything non-AuthenticationError
    # routes there.
    with (
        patch(
            "apm_cli.models.apm_package.DependencyReference.parse",
            side_effect=ValueError("simulated parse failure"),
        ),
        patch(
            "subprocess.run", side_effect=_make_credential_fill_stub(captured_stdin)
        ) as mock_subproc,
        patch("apm_cli.install.validation.requests.get", side_effect=fake_requests_get),
    ):
        result = _validate_package_exists("acme/widgets", verbose=False)

    assert result is True, "fallback validation must succeed once the good token is fetched"

    # The recovered (good) token must have been used in a GitHub API call.
    parsed_calls = [urlparse(url) for url in api_calls]
    assert any(p.path == "/repos/acme/widgets" for p in parsed_calls), (
        f"GitHub API was not called with the right repo: {api_calls!r}"
    )

    # Contract: even on the fallback branch, `path=` is sent to credential fill.
    assert captured_stdin, "git credential fill was never invoked on the fallback branch"
    last_stdin = captured_stdin[-1]
    assert "path=acme/widgets" in last_stdin, (
        "fallback (parse-failure) branch must forward path= to credential fill; "
        f"got: {last_stdin!r}"
    )

    cmd_calls = [
        call.args[0] if call.args else call.kwargs.get("args")
        for call in mock_subproc.call_args_list
    ]
    assert any(
        cmd and "credential" in " ".join(cmd) and "fill" in " ".join(cmd)
        for cmd in cmd_calls
        if isinstance(cmd, (list, tuple))
    ), f"git credential fill not in subprocess calls: {cmd_calls!r}"


def test_marketplace_fetch_threads_path_to_credential_fill(isolated_env, stub_gh_cli_unavailable):
    """E2E (marketplace/client.py:273): marketplace package fetches must thread
    `path=<owner>/<repo>` through to git credential fill so multi-account GCM
    users get the right account when downloading from upstream marketplaces.

    Verifies the marketplace -> AuthResolver.try_with_fallback -> token_manager
    chain end-to-end; complements the unit-level assertion in
    `tests/unit/marketplace/test_marketplace_client.py`.
    """
    from apm_cli.core.auth import AuthResolver

    captured_stdin: list[str] = []
    captured_paths: list[str | None] = []

    real_resolve = GitHubTokenManager.resolve_credential_from_git

    def recording_resolve(host, port=None, path=None):
        captured_paths.append(path)
        return real_resolve(host, port=port, path=path)

    def operation(token, git_env):
        # Reject any token that isn't the recovered credential-fill one --
        # forces try_with_fallback to walk the full chain (env -> gh cli ->
        # git credential fill) the same way the marketplace client does.
        if token != "good-token":
            raise RuntimeError(f"simulated 401 with non-recovered token: {token!r}")
        return f"ok:{token}"

    with (
        patch.object(
            GitHubTokenManager,
            "resolve_credential_from_git",
            side_effect=recording_resolve,
        ),
        patch("subprocess.run", side_effect=_make_credential_fill_stub(captured_stdin)),
    ):
        auth_resolver = AuthResolver()
        result = auth_resolver.try_with_fallback(
            "github.com",
            operation,
            org="acme-org",
            path="acme-org/plugins",
            unauth_first=False,
        )

    assert result == "ok:good-token"
    assert captured_paths and captured_paths[-1] == "acme-org/plugins", (
        f"marketplace path= not forwarded to resolve_credential_from_git: {captured_paths!r}"
    )
    assert captured_stdin, "git credential fill stdin was never captured"
    assert "path=acme-org/plugins" in captured_stdin[-1], (
        f"credential fill stdin missing path= for marketplace fetch: {captured_stdin[-1]!r}"
    )
