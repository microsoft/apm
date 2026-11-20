"""Tests for the apm auth command."""

import os
import subprocess
import sys
import textwrap
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.auth import (
    auth,
)

pytestmark = pytest.mark.component


class TestAuthFlow:
    def setup_method(self):
        self.runner = CliRunner()

    def test_existing_token_reported_without_network(self):
        with (
            patch("apm_cli.commands.auth.resolve_existing_token", return_value=("t", "gh-auth")),
            patch("requests.Session.get") as get,
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
        assert "Expected a bare host name" in result.output

    def test_host_with_query_string_is_rejected_before_classify(self):
        """A bare-FQDN-looking string with a query string must not reach
        ``classify_host`` -- ``attacker.example?x=.ghe.com`` must not be
        misrouted into GHE classification by a downstream substring match."""
        with patch("apm_cli.core.auth.AuthResolver") as resolver:
            result = self.runner.invoke(auth, ["attacker.example?x=.ghe.com"])
        assert result.exit_code == 1
        assert "Expected a bare host name" in result.output
        resolver.classify_host.assert_not_called()

    def test_host_with_hash_at_colon_or_whitespace_is_rejected(self):
        for bad_host in (
            "attacker.example#frag",
            "attacker.example@evil.com",
            "attacker.example:443",
            "attacker.example evil.com",
        ):
            result = self.runner.invoke(auth, [bad_host])
            assert result.exit_code == 1, bad_host
            assert "Expected a bare host name" in result.output

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
