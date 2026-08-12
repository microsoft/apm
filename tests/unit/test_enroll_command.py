"""Tests for the apm enroll command."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.enroll import (
    _host_and_project,
    _token_env_var,
    enroll,
    resolve_existing_token,
    verify_token,
)

GITLAB_SOURCE = "gitlab.com/acme/team/apm-marketplace"


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
    """`_host_and_project` must agree with `marketplace add`'s parser."""

    def test_shorthand_with_host_segment(self):
        url, host, project = _host_and_project(GITLAB_SOURCE, None)
        assert host == "gitlab.com"
        assert project == "acme/team/apm-marketplace"
        assert url.startswith("https://gitlab.com/")

    def test_full_https_url(self):
        _, host, project = _host_and_project("https://gitlab.com/acme/apm-marketplace", None)
        assert host == "gitlab.com"
        assert project == "acme/apm-marketplace"

    def test_git_suffix_stripped_from_project_path(self):
        """A .git suffix would 404 the REST project lookup."""
        _, _, project = _host_and_project("https://gitlab.com/acme/apm-marketplace.git", None)
        assert project == "acme/apm-marketplace"

    def test_local_path_yields_no_host(self, tmp_path):
        url, host, project = _host_and_project(str(tmp_path), None)
        assert url.startswith("file://")
        assert host == ""
        assert project == ""

    def test_http_source_rejected(self):
        """Insecure HTTP must not silently downgrade a marketplace fetch."""
        with pytest.raises(ValueError, match="Insecure HTTP"):
            _host_and_project("http://gitlab.com/acme/repo", None)


class TestTokenEnvVar:
    def test_gitlab(self):
        assert _token_env_var("gitlab") == "GITLAB_APM_PAT"

    def test_github_class(self):
        assert _token_env_var("github") == "GITHUB_APM_PAT"
        assert _token_env_var("ghes") == "GITHUB_APM_PAT"

    def test_unknown_host_kind(self):
        assert _token_env_var("generic") is None


class TestVerifyToken:
    """Verification must hit the REST API, since git-valid != API-valid."""

    def test_no_token_short_circuits(self):
        ok, status = verify_token(None, "gitlab.com", "acme/repo")
        assert ok is False
        assert status is None

    def test_success(self):
        with patch("requests.get") as get:
            get.return_value = MagicMock(status_code=200)
            ok, status = verify_token("glpat-x", "gitlab.com", "acme/team/repo")
        assert ok is True
        assert status == 200

    def test_oauth_token_rejected_with_401(self):
        """An OAuth session token is valid for git but not the REST API."""
        with patch("requests.get") as get:
            get.return_value = MagicMock(status_code=401)
            ok, status = verify_token("oauth-token", "gitlab.com", "acme/repo")
        assert ok is False
        assert status == 401

    def test_network_failure_is_not_a_crash(self):
        import requests

        with patch("requests.get", side_effect=requests.RequestException("boom")):
            ok, status = verify_token("glpat-x", "gitlab.com", "acme/repo")
        assert ok is False
        assert status is None

    def test_project_path_is_url_encoded(self):
        """Nested GitLab groups must collapse to %2F for the projects API."""
        with patch("requests.get") as get:
            get.return_value = MagicMock(status_code=200)
            verify_token("glpat-x", "gitlab.com", "acme/team/sub/repo")
        url = get.call_args[0][0]
        assert "acme%2Fteam%2Fsub%2Frepo" in url
        assert ".claude-plugin%2Fmarketplace.json" in url

    def test_token_sent_as_private_token_header(self):
        with patch("requests.get") as get:
            get.return_value = MagicMock(status_code=200)
            verify_token("glpat-secret", "gitlab.com", "acme/repo")
        assert get.call_args.kwargs["headers"]["PRIVATE-TOKEN"] == "glpat-secret"


class TestResolveExistingToken:
    def test_delegates_to_auth_resolver(self):
        with patch("apm_cli.core.auth.AuthResolver") as resolver:
            resolver.return_value.resolve.return_value = _auth_ctx("t", "GITLAB_APM_PAT")
            token, source = resolve_existing_token("gitlab.com")
        assert (token, source) == ("t", "GITLAB_APM_PAT")

    def test_negative_resolution_is_not_cached_across_resolvers(self, monkeypatch):
        """A failed pre-check must not poison the resolution `marketplace add` does.

        ``run_enroll`` pre-checks credentials, and on failure sets the token
        into ``os.environ`` so the subsequent registration picks it up.
        ``AuthResolver`` caches resolutions, so that only works because
        ``resolve_existing_token`` uses a *throwaway* resolver whose cache dies
        with it. If it ever holds a shared/module-level resolver instead, the
        cached ``token=None`` would win and registration would fail right after
        the user was told the token verified. This locks that invariant in.
        """
        from apm_cli.core.auth import AuthResolver

        monkeypatch.delenv("GITLAB_APM_PAT", raising=False)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)

        stale = AuthResolver()
        assert stale.resolve("gitlab.com").token is None

        monkeypatch.setenv("GITLAB_APM_PAT", "glpat-set-after-precheck")

        # What enroll's pre-check does, and what `marketplace add` does next.
        assert resolve_existing_token("gitlab.com") == (
            "glpat-set-after-precheck",
            "GITLAB_APM_PAT",
        )
        assert AuthResolver().resolve("gitlab.com").token == "glpat-set-after-precheck"

        # The same instance still serves its cached miss -- proof the cache is
        # real and that per-instance scoping is what makes the flow correct.
        assert stale.resolve("gitlab.com").token is None


class TestEnrollFlow:
    def setup_method(self):
        self.runner = CliRunner()

    def test_existing_valid_token_skips_prompt(self):
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=("t", "env")),
            patch("apm_cli.commands.enroll.verify_token", return_value=(True, 200)),
            patch("apm_cli.commands.marketplace.add") as add,
            patch("apm_cli.commands.marketplace.browse") as browse,
        ):
            result = self.runner.invoke(enroll, [GITLAB_SOURCE, "--name", "acme"])
        assert result.exit_code == 0
        assert add.call_count == 1
        assert browse.call_count == 1
        assert "Paste the token" not in result.output

    def test_non_interactive_without_token_fails_with_guidance(self):
        """CI must not hang on a prompt; it must exit telling you what to set."""
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.enroll.verify_token", return_value=(False, None)),
            patch("apm_cli.commands.enroll._is_interactive", return_value=False),
            patch("apm_cli.commands.enroll._detect_shadowing_helper", return_value=False),
        ):
            result = self.runner.invoke(enroll, [GITLAB_SOURCE, "--name", "acme"])
        assert result.exit_code == 1
        assert "GITLAB_APM_PAT" in result.output

    def test_prompted_token_is_verified_and_exported(self, monkeypatch):
        monkeypatch.delenv("GITLAB_APM_PAT", raising=False)
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.enroll.verify_token", side_effect=[(False, None), (True, 200)]),
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

    def test_bad_pasted_token_exits_nonzero(self):
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.enroll.verify_token", return_value=(False, 401)),
            patch("apm_cli.commands.enroll._is_interactive", return_value=True),
            patch("apm_cli.commands.enroll._detect_shadowing_helper", return_value=False),
            patch("apm_cli.commands.enroll._open_token_page"),
        ):
            result = self.runner.invoke(
                enroll, [GITLAB_SOURCE, "--name", "acme"], input="glpat-bad\n"
            )
        assert result.exit_code == 1
        assert "did not work" in result.output

    def test_empty_token_input_exits_nonzero(self):
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.enroll.verify_token", return_value=(False, None)),
            patch("apm_cli.commands.enroll._is_interactive", return_value=True),
            patch("apm_cli.commands.enroll._detect_shadowing_helper", return_value=False),
            patch("apm_cli.commands.enroll._open_token_page"),
        ):
            result = self.runner.invoke(enroll, [GITLAB_SOURCE, "--name", "acme"], input="\n")
        assert result.exit_code == 1
        assert "No token entered" in result.output

    def test_skip_verify_bypasses_credential_check(self):
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token") as resolve,
            patch("apm_cli.commands.marketplace.add"),
            patch("apm_cli.commands.marketplace.browse"),
        ):
            result = self.runner.invoke(enroll, [GITLAB_SOURCE, "--name", "acme", "--skip-verify"])
        assert result.exit_code == 0
        resolve.assert_not_called()

    def test_shadowing_keychain_is_reported_not_erased(self):
        """We warn about a shadowing entry; we never delete the user's credentials."""
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=(None, "none")),
            patch("apm_cli.commands.enroll.verify_token", return_value=(False, None)),
            patch("apm_cli.commands.enroll._is_interactive", return_value=False),
            patch("apm_cli.commands.enroll._detect_shadowing_helper", return_value=True),
        ):
            result = self.runner.invoke(enroll, [GITLAB_SOURCE, "--name", "acme"])
        assert "keychain" in result.output.lower()
        assert "erase" in result.output  # shown as advice for the user to run

    def test_local_source_skips_gitlab_credential_check(self, tmp_path):
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
        failing_add = MagicMock(side_effect=SystemExit(1))
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=("t", "env")),
            patch("apm_cli.commands.enroll.verify_token", return_value=(True, 200)),
            patch("apm_cli.commands.marketplace.add", failing_add),
            patch("apm_cli.commands.marketplace.browse") as browse,
        ):
            result = self.runner.invoke(enroll, [GITLAB_SOURCE, "--name", "acme"])
        assert result.exit_code == 1
        browse.assert_not_called()  # never smoke-test an unregistered marketplace

    def test_browse_failure_propagates_exit_code(self):
        """Registered but unreachable is a failure, not a success."""
        with (
            patch("apm_cli.commands.enroll.resolve_existing_token", return_value=("t", "env")),
            patch("apm_cli.commands.enroll.verify_token", return_value=(True, 200)),
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
            patch("apm_cli.commands.enroll.verify_token", return_value=(True, 200)),
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
