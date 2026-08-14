"""Tests for the apm enroll command."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.enroll import (
    _host_and_kind,
    _token_env_var,
    _token_page_url,
    _token_scopes,
    enroll,
    ensure_credential,
    resolve_existing_token,
)

GITLAB_SOURCE = "gitlab.com/acme/team/apm-marketplace"
GITHUB_SOURCE = "acme/apm-marketplace"


def _auth_ctx(token, source):
    ctx = MagicMock()
    ctx.token = token
    ctx.source = source
    return ctx


# NOTE ON PATCHING: these tests patch the delegated marketplace subcommands
# (``apm_cli.commands.marketplace.add`` / ``.browse``) rather than
# ``click.Context.invoke``. CliRunner dispatches the command under test
# *through* ``Context.invoke``, so patching that swallows the entry point and
# the command body never runs. ``run_enroll`` imports these lazily, so they
# must be patched at their definition site.


class TestSourceParsing:
    """`_host_and_kind` must agree with `marketplace add`'s parser."""

    def test_shorthand_with_host_segment(self):
        url, host, kind = _host_and_kind(GITLAB_SOURCE, None)
        assert host == "gitlab.com"
        assert kind == "gitlab"
        assert url.startswith("https://gitlab.com/")

    def test_owner_repo_shorthand_defaults_to_github(self):
        _, host, kind = _host_and_kind(GITHUB_SOURCE, None)
        assert host == "github.com"
        assert kind == "github"

    def test_full_https_url(self):
        _, host, kind = _host_and_kind("https://gitlab.com/acme/apm-marketplace", None)
        assert (host, kind) == ("gitlab.com", "gitlab")

    def test_local_path_yields_no_host(self, tmp_path):
        url, host, kind = _host_and_kind(str(tmp_path), None)
        assert url.startswith("file://")
        assert host == ""
        assert kind == "local"

    def test_http_source_rejected(self):
        """Insecure HTTP must not silently downgrade a marketplace fetch."""
        with pytest.raises(ValueError, match="Insecure HTTP"):
            _host_and_kind("http://gitlab.com/acme/repo", None)


class TestTokenEnvVar:
    def test_gitlab(self):
        assert _token_env_var("gitlab") == "GITLAB_APM_PAT"

    def test_github_class(self):
        assert _token_env_var("github") == "GITHUB_APM_PAT"
        assert _token_env_var("ghe_cloud") == "GITHUB_APM_PAT"
        assert _token_env_var("ghes") == "GITHUB_APM_PAT"

    def test_hosts_without_a_token_page(self):
        assert _token_env_var("generic") is None
        assert _token_env_var("ado") is None


class TestTokenPageAndScopes:
    def test_github_scopes_and_page(self):
        assert _token_scopes("github") == "repo"
        url = _token_page_url("github.com", "github", "apm-test")
        assert url.startswith("https://github.com/settings/tokens/new")
        assert "scopes=repo" in url

    def test_gitlab_scopes_and_page(self):
        assert _token_scopes("gitlab") == "read_repository,read_api"
        url = _token_page_url("gitlab.com", "gitlab", "apm-test")
        assert "/-/user_settings/personal_access_tokens" in url
        assert "read_repository" in url

    def test_ghes_token_page_stays_on_the_enterprise_host(self):
        """Hardcoding github.com would send enterprise users to the wrong site."""
        url = _token_page_url("ghe.corp.example", "ghes", "apm-test")
        assert url.startswith("https://ghe.corp.example/settings/tokens/new")
        assert "github.com" not in url


class TestResolveExistingToken:
    def test_delegates_to_auth_resolver(self):
        with patch("apm_cli.core.auth.AuthResolver") as resolver:
            resolver.return_value.resolve.return_value = _auth_ctx("t", "GITLAB_APM_PAT")
            token, source = resolve_existing_token("gitlab.com")
        assert (token, source) == ("t", "GITLAB_APM_PAT")

    def test_negative_resolution_is_not_cached_across_resolvers(self, monkeypatch):
        """A failed pre-check must not poison the resolution `marketplace add` does.

        ``ensure_credential`` sets a pasted token into ``os.environ`` so the
        subsequent registration picks it up. ``AuthResolver`` caches
        resolutions, so that only works because ``resolve_existing_token``
        uses a *throwaway* resolver whose cache dies with it. If it ever holds
        a shared/module-level resolver instead, the cached ``token=None``
        would win and registration would not see the new token.
        """
        from apm_cli.core.auth import AuthResolver

        monkeypatch.delenv("GITLAB_APM_PAT", raising=False)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)

        stale = AuthResolver()
        assert stale.resolve("gitlab.com").token is None

        monkeypatch.setenv("GITLAB_APM_PAT", "glpat-set-after-precheck")

        assert resolve_existing_token("gitlab.com") == (
            "glpat-set-after-precheck",
            "GITLAB_APM_PAT",
        )
        assert AuthResolver().resolve("gitlab.com").token == "glpat-set-after-precheck"

        # The same instance still serves its cached miss -- proof the cache is
        # real and that per-instance scoping is what makes the flow correct.
        assert stale.resolve("gitlab.com").token is None


class TestEnsureCredential:
    """The credential step asks only whether a token EXISTS.

    Whether it works is decided by the fetch during registration, which
    already reports auth failures precisely -- duplicating that here would
    add a second probe that could drift from it.
    """

    def setup_method(self):
        self.logger = MagicMock()

    def test_existing_token_is_accepted_without_probing(self):
        with (
            patch(
                "apm_cli.commands.enroll.resolve_existing_token",
                return_value=("t", "GITHUB_APM_PAT"),
            ),
            patch("requests.get") as get,
        ):
            assert ensure_credential(self.logger, "github.com", "github") == 0
        get.assert_not_called()  # no network call: registration is the authority

    def test_missing_token_is_not_fatal_non_interactive(self):
        """A public marketplace needs no token, so this must not hard-stop."""
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.enroll._is_interactive", return_value=False),
            patch("apm_cli.commands.enroll._detect_shadowing_helper", return_value=False),
        ):
            assert ensure_credential(self.logger, "github.com", "github") == 0
        warned = " ".join(str(c) for c in self.logger.warning.call_args_list)
        assert "GITHUB_APM_PAT" in warned

    def test_hosts_without_a_token_page_are_skipped(self):
        """ADO/generic resolve differently and have no page to point at."""
        with patch("apm_cli.commands.enroll.resolve_existing_token") as resolve:
            assert ensure_credential(self.logger, "dev.azure.com", "ado") == 0
        resolve.assert_not_called()

    def test_shadowing_keychain_is_reported_not_erased(self):
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.enroll._is_interactive", return_value=False),
            patch("apm_cli.commands.enroll._detect_shadowing_helper", return_value=True),
        ):
            ensure_credential(self.logger, "gitlab.com", "gitlab")
        warned = " ".join(str(c) for c in self.logger.warning.call_args_list)
        assert "keychain" in warned.lower()
        assert "erase" in warned  # shown as advice for the user to run


class TestEnrollFlow:
    def setup_method(self):
        self.runner = CliRunner()

    def test_existing_token_skips_prompt(self):
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=("t", "env")),
            patch("apm_cli.commands.marketplace.add") as add,
            patch("apm_cli.commands.marketplace.browse") as browse,
        ):
            result = self.runner.invoke(enroll, [GITLAB_SOURCE, "--name", "acme"])
        assert result.exit_code == 0
        assert add.call_count == 1
        assert browse.call_count == 1
        assert "Paste the token" not in result.output

    def test_non_interactive_never_prompts(self):
        """The CI guard's core promise: no prompt, so no hang."""
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.enroll._is_interactive", return_value=False),
            patch("apm_cli.commands.enroll._detect_shadowing_helper", return_value=False),
            patch("apm_cli.commands.enroll._open_token_page") as page,
            patch("apm_cli.commands.marketplace.add"),
            patch("apm_cli.commands.marketplace.browse"),
        ):
            result = self.runner.invoke(enroll, [GITLAB_SOURCE, "--name", "acme"])
        page.assert_not_called()
        assert result.exit_code == 0

    def test_pasted_token_is_exported_for_registration(self, monkeypatch):
        monkeypatch.delenv("GITLAB_APM_PAT", raising=False)
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.enroll._is_interactive", return_value=True),
            patch("apm_cli.commands.enroll._detect_shadowing_helper", return_value=False),
            patch("apm_cli.commands.enroll._open_token_page"),
            patch("apm_cli.commands.marketplace.add"),
            patch("apm_cli.commands.marketplace.browse"),
        ):
            result = self.runner.invoke(
                enroll, [GITLAB_SOURCE, "--name", "acme"], input="glpat-good\n"
            )
            import os

            assert os.environ.get("GITLAB_APM_PAT") == "glpat-good"
        monkeypatch.delenv("GITLAB_APM_PAT", raising=False)
        assert result.exit_code == 0

    def test_empty_token_input_exits_nonzero(self):
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.enroll._is_interactive", return_value=True),
            patch("apm_cli.commands.enroll._detect_shadowing_helper", return_value=False),
            patch("apm_cli.commands.enroll._open_token_page"),
        ):
            result = self.runner.invoke(enroll, [GITLAB_SOURCE, "--name", "acme"], input="\n")
        assert result.exit_code == 1
        assert "No token entered" in result.output

    def test_no_token_flag_bypasses_credential_step(self):
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token") as resolve,
            patch("apm_cli.commands.marketplace.add"),
            patch("apm_cli.commands.marketplace.browse"),
        ):
            result = self.runner.invoke(enroll, [GITLAB_SOURCE, "--name", "acme", "--no-token"])
        assert result.exit_code == 0
        resolve.assert_not_called()

    def test_local_source_skips_credential_step(self, tmp_path):
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token") as resolve,
            patch("apm_cli.commands.marketplace.add"),
            patch("apm_cli.commands.marketplace.browse"),
        ):
            result = self.runner.invoke(enroll, [str(tmp_path), "--name", "scratch"])
        assert result.exit_code == 0
        resolve.assert_not_called()

    def test_registration_failure_propagates_exit_code(self):
        """A failed 'marketplace add' must not be reported as a success."""
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=("t", "env")),
            patch("apm_cli.commands.marketplace.add", MagicMock(side_effect=SystemExit(1))),
            patch("apm_cli.commands.marketplace.browse") as browse,
        ):
            result = self.runner.invoke(enroll, [GITLAB_SOURCE, "--name", "acme"])
        assert result.exit_code == 1
        browse.assert_not_called()  # never smoke-test an unregistered marketplace

    def test_browse_failure_propagates_exit_code(self):
        """Registered but unreachable is a failure, not a success."""
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=("t", "env")),
            patch("apm_cli.commands.marketplace.add"),
            patch("apm_cli.commands.marketplace.browse", MagicMock(side_effect=SystemExit(1))),
        ):
            result = self.runner.invoke(enroll, [GITLAB_SOURCE, "--name", "acme"])
        assert result.exit_code == 1
        assert "not browsable" in result.output

    def test_alias_recovered_from_registry_when_name_omitted(self):
        """Without --name, browse must target whatever add actually registered."""
        registered = MagicMock()
        registered.name = "apm-marketplace"
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=("t", "env")),
            patch("apm_cli.commands.marketplace.add"),
            patch("apm_cli.commands.marketplace.browse"),
            patch(
                "apm_cli.marketplace.registry.get_registered_marketplaces",
                return_value=[registered],
            ),
        ):
            result = self.runner.invoke(enroll, [GITLAB_SOURCE])
        assert result.exit_code == 0
        assert "apm-marketplace" in result.output

    def test_invalid_source_exits_nonzero(self):
        result = self.runner.invoke(enroll, ["http://gitlab.com/acme/repo"])
        assert result.exit_code == 1
        assert "Invalid source" in result.output

    def test_no_manifest_probe_is_performed(self):
        """enroll must not duplicate marketplace.client's fetch logic.

        _auto_detect_path already walks every candidate manifest path and
        already distinguishes 'missing' from 'unauthenticated'. A second
        probe here could drift from it and would need to answer 'is this
        public?', which GitHub's 60/hour anonymous cap makes unreliable.
        """
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=("t", "env")),
            patch("apm_cli.commands.marketplace.add"),
            patch("apm_cli.commands.marketplace.browse"),
            patch("requests.get") as get,
        ):
            result = self.runner.invoke(enroll, [GITHUB_SOURCE, "--name", "acme"])
        assert result.exit_code == 0
        get.assert_not_called()
