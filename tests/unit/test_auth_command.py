"""Tests for the apm auth command."""

import os
import subprocess
import sys
import textwrap
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from click.testing import CliRunner

from apm_cli.commands.auth import (
    auth,
    check_token,
    resolve_existing_token,
    token_env_var,
    token_page_url,
    token_scopes,
)


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
        with patch("requests.get") as get:
            get.return_value = MagicMock(status_code=200)
            verdict, status = check_token("ghp_x", "github.com", "github")
        assert (verdict, status) == ("ok", 200)
        assert get.call_args.kwargs["headers"]["Authorization"] == "token ghp_x"
        called = urlparse(get.call_args[0][0])
        assert (called.hostname, called.path) == ("api.github.com", "/user")

    def test_gitlab_uses_private_token_header(self):
        with patch("requests.get") as get:
            get.return_value = MagicMock(status_code=200)
            check_token("glpat-x", "gitlab.com", "gitlab")
        assert get.call_args.kwargs["headers"]["PRIVATE-TOKEN"] == "glpat-x"
        assert "PRIVATE-TOKEN" in get.call_args.kwargs["headers"]

    def test_oauth_token_is_rejected(self):
        """A GitLab OAuth session token is git-valid but not REST-valid."""
        with patch("requests.get") as get:
            get.return_value = MagicMock(status_code=401)
            verdict, status = check_token("oauth", "gitlab.com", "gitlab")
        assert (verdict, status) == ("rejected", 401)

    def test_ghes_uses_the_enterprise_api_base(self):
        with patch("requests.get") as get:
            get.return_value = MagicMock(status_code=200)
            check_token("ghp_x", "ghe.corp.example", "ghes")
        called = urlparse(get.call_args[0][0])
        assert called.hostname == "ghe.corp.example"
        assert called.path == "/api/v3/user"

    def test_unreachable_api_is_indeterminate_not_rejected(self):
        """A plane / proxy / captive portal must not read as a bad token."""
        import requests

        with patch("requests.get", side_effect=requests.RequestException("boom")):
            verdict, status = check_token("t", "github.com", "github")
        assert (verdict, status) == ("indeterminate", None)

    def test_server_error_is_indeterminate(self):
        """A 502 says nothing about the credential."""
        with patch("requests.get") as get:
            get.return_value = MagicMock(status_code=502)
            verdict, status = check_token("glpat-x", "gitlab.com", "gitlab")
        assert (verdict, status) == ("indeterminate", 502)

    def test_github_app_token_403_is_indeterminate(self):
        """Actions' GITHUB_TOKEN (ghs_) has no user context.

        GET /user answers 403 for an installation token even though it reads
        repository contents fine -- which is what marketplace lookups use. A
        pre-flight `apm auth github.com --check` must not fail the job.
        """
        with patch("requests.get") as get:
            get.return_value = MagicMock(status_code=403)
            verdict, status = check_token("ghs_actions", "github.com", "github")
        assert (verdict, status) == ("indeterminate", 403)

    def test_plain_pat_403_is_still_rejected(self):
        """The app-token carve-out must not swallow a genuine refusal."""
        with patch("requests.get") as get:
            get.return_value = MagicMock(status_code=403)
            verdict, status = check_token("ghp_x", "github.com", "github")
        assert (verdict, status) == ("rejected", 403)


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


class TestAuthFlow:
    def setup_method(self):
        self.runner = CliRunner()

    def test_existing_token_reported_without_network(self):
        with (
            patch("apm_cli.commands.auth.resolve_existing_token", return_value=("t", "gh-auth")),
            patch("requests.get") as get,
        ):
            result = self.runner.invoke(auth, ["github.com"])
        assert result.exit_code == 0
        get.assert_not_called()  # no --check means no round trip
        assert "gh-auth" in result.output

    def test_check_validates_and_reports(self):
        with (
            patch("apm_cli.commands.auth.resolve_existing_token", return_value=("t", "env")),
            patch("apm_cli.commands.auth.check_token", return_value=("ok", 200)),
        ):
            result = self.runner.invoke(auth, ["github.com", "--check"])
        assert result.exit_code == 0
        assert "works" in result.output

    def test_rejected_gitlab_token_explains_oauth(self):
        """The whole point: say WHY, not just that it failed."""
        with (
            patch(
                "apm_cli.commands.auth.resolve_existing_token",
                return_value=("oauth", "GITLAB_APM_PAT"),
            ),
            patch("apm_cli.commands.auth.check_token", return_value=("rejected", 401)),
            patch("apm_cli.commands.auth.is_interactive", return_value=False),
            patch("apm_cli.commands.auth.detect_shadowing_helper", return_value=False),
        ):
            result = self.runner.invoke(auth, ["gitlab.com", "--check"])
        assert result.exit_code == 1
        assert "OAuth" in result.output
        assert "personal access token" in result.output

    def test_unvalidatable_credential_is_kept_not_replaced(self):
        """comment: unreachable API must not read as a rejected credential.

        Non-interactive (CI) with a resolved token that cannot be validated:
        the job must not fail, and the user must not be told to mint a PAT.
        """
        with (
            patch(
                "apm_cli.commands.auth.resolve_existing_token",
                return_value=("ghs_actions", "GITHUB_TOKEN"),
            ),
            patch("apm_cli.commands.auth.is_interactive", return_value=False),
            patch("apm_cli.commands.auth.check_token", return_value=("indeterminate", 403)),
        ):
            result = self.runner.invoke(auth, ["github.com", "--check"])
        assert result.exit_code == 0
        assert "rejected" not in result.output

    def test_unvalidatable_credential_still_exports(self):
        """The CI path: GITHUB_TOKEN that cannot be validated must still export.

        Short-circuiting on 'indeterminate' must not drop the export line --
        that is the branch an Actions job running --check --export lands on.
        """
        with (
            patch(
                "apm_cli.commands.auth.resolve_existing_token",
                return_value=("ghs_actions", "GITHUB_TOKEN"),
            ),
            patch("apm_cli.commands.auth.is_interactive", return_value=False),
            patch("apm_cli.commands.auth.check_token", return_value=("indeterminate", 403)),
        ):
            result = self.runner.invoke(auth, ["github.com", "--check", "--export"])
        assert result.exit_code == 0
        assert "export GITHUB_APM_PAT='ghs_actions'" in result.stdout

    def test_non_interactive_without_token_exits_1(self):
        with (
            patch("apm_cli.commands.auth.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.auth.is_interactive", return_value=False),
            patch("apm_cli.commands.auth.detect_shadowing_helper", return_value=False),
        ):
            result = self.runner.invoke(auth, ["gitlab.com"])
        assert result.exit_code == 1
        assert "GITLAB_APM_PAT" in result.output
        assert "Paste the token" not in result.output  # never prompted

    def test_prompted_token_is_accepted(self):
        with (
            patch("apm_cli.commands.auth.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.auth.is_interactive", return_value=True),
            patch("apm_cli.commands.auth.detect_shadowing_helper", return_value=False),
            patch("apm_cli.commands.auth.open_token_page"),
        ):
            result = self.runner.invoke(auth, ["gitlab.com"], input="glpat-good\n")
        assert result.exit_code == 0
        assert "GITLAB_APM_PAT" in result.output

    def test_empty_paste_exits_1(self):
        with (
            patch("apm_cli.commands.auth.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.auth.is_interactive", return_value=True),
            patch("apm_cli.commands.auth.detect_shadowing_helper", return_value=False),
            patch("apm_cli.commands.auth.open_token_page"),
        ):
            result = self.runner.invoke(auth, ["gitlab.com"], input="\n")
        assert result.exit_code == 1
        assert "No token entered" in result.output

    def test_bad_pasted_token_with_check_exits_1(self):
        with (
            patch("apm_cli.commands.auth.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.auth.is_interactive", return_value=True),
            patch("apm_cli.commands.auth.detect_shadowing_helper", return_value=False),
            patch("apm_cli.commands.auth.open_token_page"),
            patch("apm_cli.commands.auth.check_token", return_value=("rejected", 401)),
        ):
            result = self.runner.invoke(auth, ["gitlab.com", "--check"], input="bad\n")
        assert result.exit_code == 1
        assert "rejected" in result.output

    def test_a_repo_path_is_rejected(self):
        """HOST is a host, not a marketplace source -- catch the confusion early."""
        result = self.runner.invoke(auth, ["gitlab.com/acme/repo"])
        assert result.exit_code == 1
        assert "Expected a host name" in result.output

    def test_host_without_a_token_flow_exits_1(self):
        result = self.runner.invoke(auth, ["dev.azure.com"])
        assert result.exit_code == 1
        assert "No token flow" in result.output

    def test_generic_host_error_names_the_env_var_to_set(self):
        """'host class generic' is not actionable; GITHUB_HOST=<host> is.

        ghe.corp.example is the example the docs offer, and it lands in
        'generic' until its env hint is set.
        """
        with patch("apm_cli.commands.auth.resolve_existing_token", return_value=(None, "none")):
            # 'generic' only holds while no env hint claims the host, and
            # ghe.corp.example is the very value the docs tell people to
            # export -- so a developer with it set would otherwise see this
            # test fail for a reason that has nothing to do with the code.
            result = self.runner.invoke(
                auth,
                ["ghe.corp.example"],
                env={"GITHUB_HOST": None, "GITLAB_HOST": None, "APM_GITLAB_HOSTS": None},
            )
        assert result.exit_code == 1
        assert "GITHUB_HOST=ghe.corp.example" in result.output
        assert "GITLAB_HOST=ghe.corp.example" in result.output

    def test_shadowing_warning_is_suppressed_for_env_var_credentials(self):
        """Env vars beat helpers in _resolve_token, so nothing was shadowed.

        Advising a keychain erase here would destroy the credential plain git
        uses, to fix a problem the keychain was not causing.
        """
        with (
            patch(
                "apm_cli.commands.auth.resolve_existing_token",
                return_value=("glpat-underscoped", "GITLAB_APM_PAT"),
            ),
            patch("apm_cli.commands.auth.check_token", return_value=("rejected", 403)),
            patch("apm_cli.commands.auth.detect_shadowing_helper", return_value=True),
            patch("apm_cli.commands.auth.is_interactive", return_value=False),
        ):
            result = self.runner.invoke(auth, ["gitlab.com", "--check"])
        assert "keychain" not in result.output
        assert "osxkeychain erase" not in result.output

    def test_shadowing_warning_still_fires_for_helper_credentials(self):
        """The genuinely useful case must survive the gate."""
        with (
            patch(
                "apm_cli.commands.auth.resolve_existing_token",
                return_value=("stale", "git-credential-fill"),
            ),
            patch("apm_cli.commands.auth.check_token", return_value=("rejected", 401)),
            patch("apm_cli.commands.auth.detect_shadowing_helper", return_value=True),
            patch("apm_cli.commands.auth.is_interactive", return_value=False),
        ):
            result = self.runner.invoke(auth, ["gitlab.com", "--check"])
        assert "keychain" in result.output

    def test_shadowing_keychain_is_reported_not_erased(self):
        with (
            patch("apm_cli.commands.auth.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.auth.is_interactive", return_value=False),
            patch("apm_cli.commands.auth.detect_shadowing_helper", return_value=True),
        ):
            result = self.runner.invoke(auth, ["gitlab.com"])
        assert "keychain" in result.output.lower()
        assert "erase" in result.output  # advice for the user to run


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


class TestExportMode:
    """`eval "$(apm auth <host> --export)"` requires a clean stdout."""

    def setup_method(self):
        # Click >= 8.2 keeps stdout and stderr separate by default; the old
        # mix_stderr=False argument was removed.
        self.runner = CliRunner()

    def test_stdout_carries_only_the_export_line(self):
        with patch(
            "apm_cli.commands.auth.resolve_existing_token",
            return_value=("glpat-x", "GITLAB_APM_PAT"),
        ):
            result = self.runner.invoke(auth, ["gitlab.com", "--export"])
        assert result.exit_code == 0
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert lines == ["export GITLAB_APM_PAT='glpat-x'"]

    def test_narration_goes_to_stderr(self):
        with patch(
            "apm_cli.commands.auth.resolve_existing_token",
            return_value=("glpat-x", "GITLAB_APM_PAT"),
        ):
            result = self.runner.invoke(auth, ["gitlab.com", "--export"])
        assert "GITLAB_APM_PAT" in result.stderr or "credential" in result.stderr
        assert "[+]" not in result.stdout

    def test_subprocess_chatter_cannot_reach_the_eval(self):
        """A real subprocess: the only shape that exercises fd 1.

        ``CliRunner`` captures ``sys.stdout``, so it cannot see the file
        descriptor that ``webbrowser.open``/``gh``/``git credential`` inherit.
        A helper that printed to a chatty BROWSER used to land on the real
        stdout ahead of the export line, where ``eval`` would run it.
        """
        script = textwrap.dedent(
            """
            import sys
            from unittest.mock import patch
            with patch(
                "apm_cli.commands.auth.resolve_existing_token",
                return_value=(None, "none"),
            ), patch("apm_cli.commands.auth.is_interactive", return_value=True), patch(
                "click.prompt", return_value="glpat-pasted"
            ):
                from apm_cli.commands.auth import auth
                try:
                    auth.main(["gitlab.com", "--export"], standalone_mode=False)
                except SystemExit as exc:
                    sys.exit(exc.code or 0)
            """
        )
        env = {
            **os.environ,
            # webbrowser treats this as a BackgroundBrowser and inherits fd 1.
            "BROWSER": "/bin/echo",
            "PYTHONPATH": os.pathsep.join(sys.path),
        }
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        assert lines == ["export GITLAB_APM_PAT='glpat-pasted'"], (
            f"stdout must carry only the export line, got: {proc.stdout!r}"
        )

    def test_single_quote_in_token_cannot_break_out_of_the_quoting(self):
        """The output is eval'd, so a quote must not become shell syntax."""
        with patch(
            "apm_cli.commands.auth.resolve_existing_token",
            return_value=("ab'; echo pwned; '", "GITLAB_APM_PAT"),
        ):
            result = self.runner.invoke(auth, ["gitlab.com", "--export"])
        line = result.stdout.strip()
        assert "'\\''" in line  # POSIX close-escape-reopen
        assert line.startswith("export GITLAB_APM_PAT='")
        assert line.endswith("'")
