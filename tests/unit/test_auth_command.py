"""Tests for the apm auth command."""

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from apm_cli.commands.auth import (
    check_token,
    resolve_existing_token,
    token_env_var,
    token_page_url,
    token_scopes,
)

pytestmark = pytest.mark.unit


def _auth_ctx(token, source):
    ctx = MagicMock()
    ctx.token = token
    ctx.source = source
    return ctx


class TestHostMapping:
    def test_gitlab(self):
        assert token_env_var("gitlab") == "GITLAB_APM_PAT"
        assert token_scopes("gitlab") == "read_repository,read_api"

    def test_github_class(self):
        for kind in ("github", "ghe_cloud", "ghes"):
            assert token_env_var(kind) == "GITHUB_APM_PAT"
            assert token_scopes(kind) == "repo"

    def test_hosts_without_a_token_flow(self):
        assert token_env_var("generic") is None
        assert token_env_var("ado") is None

    def test_github_token_page(self):
        url = urlparse(token_page_url("github.com", "github", "apm-test"))
        assert (url.scheme, url.hostname, url.path) == (
            "https",
            "github.com",
            "/settings/tokens/new",
        )
        assert parse_qs(url.query)["scopes"] == ["repo"]

    def test_gitlab_token_page(self):
        url = urlparse(token_page_url("gitlab.com", "gitlab", "apm-test"))
        assert (url.scheme, url.hostname, url.path) == (
            "https",
            "gitlab.com",
            "/-/user_settings/personal_access_tokens",
        )
        assert parse_qs(url.query)["scopes"] == ["read_repository,read_api"]

    def test_ghes_page_stays_on_the_enterprise_host(self):
        """Hardcoding github.com would send enterprise users to the wrong site."""
        url = urlparse(token_page_url("ghe.corp.example", "ghes", "apm-test"))
        # Hostname equality, not "github.com not in url": the parsed form also
        # rules out a look-alike host that a substring check would accept.
        assert url.hostname == "ghe.corp.example"
        assert url.path == "/settings/tokens/new"


class TestCheckToken:
    """--check hits the identity endpoint, so the answer does not depend on
    access to any particular repository."""

    def test_github_uses_token_header(self):
        with patch("requests.Session.get") as get:
            get.return_value = MagicMock(status_code=200)
            verdict, status = check_token("ghp_x", "github.com", "github")
        assert (verdict, status) == ("ok", 200)
        assert get.call_args.kwargs["headers"]["Authorization"] == "token ghp_x"
        called = urlparse(get.call_args[0][0])
        assert (called.hostname, called.path) == ("api.github.com", "/user")

    def test_gitlab_uses_private_token_header(self):
        with patch("requests.Session.get") as get:
            get.return_value = MagicMock(status_code=200)
            check_token("glpat-x", "gitlab.com", "gitlab")
        assert get.call_args.kwargs["headers"]["PRIVATE-TOKEN"] == "glpat-x"
        assert "PRIVATE-TOKEN" in get.call_args.kwargs["headers"]

    def test_oauth_token_is_rejected(self):
        """A GitLab OAuth session token is git-valid but not REST-valid."""
        with patch("requests.Session.get") as get:
            get.return_value = MagicMock(status_code=401)
            verdict, status = check_token("oauth", "gitlab.com", "gitlab")
        assert (verdict, status) == ("rejected", 401)

    def test_ghes_uses_the_enterprise_api_base(self):
        with patch("requests.Session.get") as get:
            get.return_value = MagicMock(status_code=200)
            check_token("ghp_x", "ghe.corp.example", "ghes")
        called = urlparse(get.call_args[0][0])
        assert called.hostname == "ghe.corp.example"
        assert called.path == "/api/v3/user"

    def test_unreachable_api_is_indeterminate_not_rejected(self):
        """A plane / proxy / captive portal must not read as a bad token."""
        import requests

        with patch("requests.Session.get", side_effect=requests.RequestException("boom")):
            verdict, status = check_token("t", "github.com", "github")
        assert (verdict, status) == ("indeterminate", None)

    def test_server_error_is_indeterminate(self):
        """A 502 says nothing about the credential."""
        with patch("requests.Session.get") as get:
            get.return_value = MagicMock(status_code=502)
            verdict, status = check_token("glpat-x", "gitlab.com", "gitlab")
        assert (verdict, status) == ("indeterminate", 502)

    def test_github_app_token_403_is_indeterminate(self):
        """Actions' GITHUB_TOKEN (ghs_) has no user context.

        GET /user answers 403 for an installation token even though it reads
        repository contents fine -- which is what marketplace lookups use. A
        pre-flight `apm auth github.com --check` must not fail the job.
        """
        with patch("requests.Session.get") as get:
            get.return_value = MagicMock(status_code=403)
            verdict, status = check_token("ghs_actions", "github.com", "github")
        assert (verdict, status) == ("indeterminate", 403)

    def test_plain_pat_403_is_still_rejected(self):
        """The app-token carve-out must not swallow a genuine refusal."""
        with patch("requests.Session.get") as get:
            get.return_value = MagicMock(status_code=403)
            verdict, status = check_token("ghp_x", "github.com", "github")
        assert (verdict, status) == ("rejected", 403)

    def test_redirect_is_indeterminate_not_followed(self):
        """A 3xx means the probe did not actually get an answer.

        Following it could send the token to a host the user never asked
        about, so the request must be made with allow_redirects=False and a
        redirect must read as indeterminate, never ok/rejected.
        """
        with patch("requests.Session.get") as get:
            get.return_value = MagicMock(status_code=302)
            verdict, status = check_token("ghp_x", "github.com", "github")
        assert (verdict, status) == ("indeterminate", 302)
        assert get.call_args.kwargs["allow_redirects"] is False

    def test_moved_permanently_is_also_indeterminate(self):
        with patch("requests.Session.get") as get:
            get.return_value = MagicMock(status_code=301)
            verdict, status = check_token("ghp_x", "github.com", "github")
        assert (verdict, status) == ("indeterminate", 301)

    def test_request_disables_redirects_and_ambient_env(self):
        """The probe must not follow redirects or trust .netrc/proxy env.

        A malicious or misconfigured host could otherwise redirect the
        validation request elsewhere, or have the token request pick up
        credentials/proxies from the environment it was never meant to use.
        """
        with patch("requests.Session") as session_cls:
            session = session_cls.return_value
            session.get.return_value = MagicMock(status_code=200)
            check_token("ghp_x", "github.com", "github")

        assert session.get.call_args.kwargs["allow_redirects"] is False
        assert session.get.call_args.kwargs["timeout"] == 15
        assert session.trust_env is False


class TestResolveExistingToken:
    def test_delegates_to_auth_resolver(self):
        with patch("apm_cli.core.auth.AuthResolver") as resolver:
            resolver.return_value.resolve.return_value = _auth_ctx("t", "GITLAB_APM_PAT")
            assert resolve_existing_token("gitlab.com") == ("t", "GITLAB_APM_PAT")

    def test_negative_resolution_is_not_cached_across_resolvers(self, monkeypatch):
        """AuthResolver caches per instance; a retained one would serve a stale miss."""
        from apm_cli.core.auth import AuthResolver

        monkeypatch.delenv("GITLAB_APM_PAT", raising=False)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)

        stale = AuthResolver()
        assert stale.resolve("gitlab.com").token is None

        monkeypatch.setenv("GITLAB_APM_PAT", "glpat-set-after-lookup")
        assert resolve_existing_token("gitlab.com")[0] == "glpat-set-after-lookup"
        assert stale.resolve("gitlab.com").token is None  # cache is real


class TestDocumentedResolutionChain:
    """The resolution chain is stated in three places; they must agree.

    `GH_TOKEN` was missing from all three -- the docs table, the module
    docstring, and the --help epilog -- while `_resolve_token` has read it all
    along via TOKEN_PRECEDENCE["modules"]. A user with only GH_TOKEN exported
    reads any of them as "not used" and mints a PAT they do not need.
    """

    def test_help_epilog_names_every_github_env_var_apm_reads(self):
        from apm_cli.commands.auth import _EPILOG
        from apm_cli.core.token_manager import GitHubTokenManager

        for var in GitHubTokenManager.TOKEN_PRECEDENCE["modules"]:
            assert var in _EPILOG, f"{var} is read by APM but absent from --help"

    def test_module_docstring_names_every_github_env_var_apm_reads(self):
        import apm_cli.commands.auth as auth_mod
        from apm_cli.core.token_manager import GitHubTokenManager

        for var in GitHubTokenManager.TOKEN_PRECEDENCE["modules"]:
            assert var in auth_mod.__doc__, f"{var} is read by APM but absent from the docstring"
