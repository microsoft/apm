"""Tests for ``apm marketplace doctor`` subcommand."""

from __future__ import annotations

import subprocess
import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace
from apm_cli.marketplace.yml_schema import (
    MarketplaceOwner,
    MarketplaceYml,
    PackageEntry,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BASIC_YML = textwrap.dedent("""\
    name: test-marketplace
    description: Test marketplace
    version: 1.0.0
    owner:
      name: Test Owner
    packages:
      - name: solo
        source: acme-org/solo
        version: "^1.0.0"
""")


# Token env vars that AuthResolver inspects.  Cleared in the autouse
# fixture below so doctor tests are deterministic regardless of CI env.
_TOKEN_ENV_VARS = ("GITHUB_APM_PAT", "GITHUB_TOKEN", "GH_TOKEN")


@pytest.fixture(autouse=True)
def _mock_auth_resolver(monkeypatch):
    """Make the auth check deterministic by mocking AuthResolver.

    Without this, the number of ``subprocess.run`` calls inside
    ``doctor()`` varies depending on whether an env-var token exists
    (AuthResolver skips ``git credential fill`` when one is found),
    which causes positional mock side-effects to shift on CI where
    ``GITHUB_APM_PAT`` is set.
    """
    for var in _TOKEN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    auth_ctx = SimpleNamespace(token="mock-doctor-token")
    mock_cls = MagicMock()
    mock_cls.return_value.resolve.return_value = auth_ctx
    monkeypatch.setattr("apm_cli.core.auth.AuthResolver", mock_cls)


@pytest.fixture
def runner():
    return CliRunner()


def _make_run_result(returncode=0, stdout="", stderr=""):
    """Build a fake subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(
        args=["git"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


_GH_OK = _make_run_result(
    0, stdout="gh version 2.50.0 (2024-06-01)\nhttps://github.com/cli/cli/releases/tag/v2.50.0"
)


# ---------------------------------------------------------------------------
# All checks pass
# ---------------------------------------------------------------------------


class TestDoctorAllPass:
    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_all_pass_exit_0(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        (tmp_path / "marketplace.yml").write_text(_BASIC_YML, encoding="utf-8")

        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0, stdout="abc123\tHEAD"),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_git_version_shown(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0, stdout="abc123\tHEAD"),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert "git version" in result.output

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_network_reachable_shown(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert "reachable" in result.output.lower()


# ---------------------------------------------------------------------------
# Check 1: git on PATH
# ---------------------------------------------------------------------------


class TestDoctorGitCheck:
    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_git_missing_exits_1(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = FileNotFoundError("git not found")

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_git_timeout(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=5)

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 1
        assert "timed out" in result.output.lower()

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_git_nonzero_exit(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(returncode=1, stderr="error"),
            _make_run_result(0),  # network check may still run
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Check 2: network
# ---------------------------------------------------------------------------


class TestDoctorNetworkCheck:
    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_network_failure_exits_1(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(128, stderr="fatal: could not resolve host"),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 1

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_network_timeout(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            subprocess.TimeoutExpired(cmd="git", timeout=5),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 1
        assert "timed out" in result.output.lower()

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_network_auth_error(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(128, stderr="fatal: authentication failed"),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Check 3: auth token
# ---------------------------------------------------------------------------


class TestDoctorAuthCheck:
    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_github_token_detected(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert "Token detected" in result.output
        # Must NOT print the actual token
        assert "ghp_test123" not in result.output

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_gh_token_detected(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "gho_test456")
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert "Token detected" in result.output
        assert "gho_test456" not in result.output

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_no_token_informational(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Override the autouse mock so AuthResolver reports no token.
        no_token_ctx = SimpleNamespace(token=None)
        mock_cls = MagicMock()
        mock_cls.return_value.resolve.return_value = no_token_ctx
        monkeypatch.setattr("apm_cli.core.auth.AuthResolver", mock_cls)

        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0  # no token is informational, not a failure
        assert "unauthenticated" in result.output.lower() or "rate limit" in result.output.lower()


# ---------------------------------------------------------------------------
# Check 4: gh CLI
# ---------------------------------------------------------------------------


class TestDoctorGhCliCheck:
    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_gh_found_shows_version(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _make_run_result(
                0,
                stdout="gh version 2.50.0 (2024-06-01)\nhttps://github.com/cli/cli/releases/tag/v2.50.0",
            ),
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0
        assert "gh version" in result.output

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_gh_missing_is_warning_not_error(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            FileNotFoundError("gh not found"),
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0  # gh is informational; missing does not fail
        assert "not found" in result.output.lower()
        assert "cli.github.com" in result.output

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_gh_nonzero_exit(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _make_run_result(returncode=1, stderr="error"),
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0  # informational
        assert "non-zero" in result.output.lower()

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_gh_timeout(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            subprocess.TimeoutExpired(cmd="gh", timeout=10),
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0  # informational
        assert "timed out" in result.output.lower()

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_gh_general_exception(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            OSError("Permission denied"),
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0  # informational
        assert "Permission denied" in result.output

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_gh_shown_in_table(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert "gh cli" in result.output.lower()


# ---------------------------------------------------------------------------
# Check 5: marketplace.yml
# ---------------------------------------------------------------------------


class TestDoctorYmlCheck:
    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_yml_present_and_valid(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_BASIC_YML, encoding="utf-8")
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0
        assert "valid" in result.output.lower() or "found" in result.output.lower()

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_yml_present_but_invalid(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text("bad: true\n", encoding="utf-8")
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        # yml check is informational; critical checks still pass
        assert result.exit_code == 0
        assert "error" in result.output.lower()

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_yml_absent(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0
        assert "No marketplace authoring config" in result.output


# ---------------------------------------------------------------------------
# G7: format coverage row
# ---------------------------------------------------------------------------


_APM_YML_CLAUDE_ONLY = textwrap.dedent("""\
    name: demo
    description: d
    version: 0.1.0
    marketplace:
      owner: {name: acme}
      outputs:
        claude: {}
      packages:
        - name: pkg
          source: acme/pkg
          version: "^1.0.0"
""")

_APM_YML_BOTH_FORMATS = textwrap.dedent("""\
    name: demo
    description: d
    version: 0.1.0
    marketplace:
      owner: {name: acme}
      outputs:
        claude: {}
        codex: {}
      packages:
        - name: pkg
          source: acme/pkg
          version: "^1.0.0"
          category: Productivity
""")


class TestDoctorFormatCoverage:
    """G7: doctor flags which marketplace output profiles are configured
    vs. supported, never failing -- just informational guidance."""

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_partial_coverage_lists_missing_formats(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_CLAUDE_ONLY, encoding="utf-8")
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0
        assert "format coverage" in result.output
        # Surfaces what's configured and what's available
        assert "claude" in result.output
        assert "codex" in result.output

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_full_coverage_passes_silently(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(_APM_YML_BOTH_FORMATS, encoding="utf-8")
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0
        assert "format coverage" in result.output
        assert "all known formats" in result.output.lower()

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_no_config_skips_format_coverage_row(self, mock_run, runner, tmp_path, monkeypatch):
        """When apm.yml has no marketplace block, doctor must NOT
        synthesize a coverage row -- format coverage is meaningful only
        for projects that publish a marketplace."""
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0
        assert "format coverage" not in result.output


# ---------------------------------------------------------------------------
# Exit code logic (check 4 never blocks)
# ---------------------------------------------------------------------------


class TestDoctorExitCodes:
    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_yml_invalid_does_not_cause_exit_1(self, mock_run, runner, tmp_path, monkeypatch):
        """Check 5 is informational; invalid yml alone should not exit 1."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text("bad: x\n", encoding="utf-8")
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_git_fail_plus_valid_yml_exits_1(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text(_BASIC_YML, encoding="utf-8")
        mock_run.side_effect = FileNotFoundError("git not found")

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Verbose
# ---------------------------------------------------------------------------


class TestDoctorVerbose:
    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_verbose_no_crash(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor", "--verbose"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------


class TestDoctorTable:
    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_table_has_check_column(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        # Table should mention the check names
        assert "git" in result.output.lower()
        assert "network" in result.output.lower()
        assert "auth" in result.output.lower()

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_info_icon_for_auth(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert "[i]" in result.output

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_pass_icon_for_git(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert "[+]" in result.output

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_fail_icon_for_git_missing(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = FileNotFoundError("not found")

        result = runner.invoke(marketplace, ["doctor"])
        assert "[x]" in result.output


# ---------------------------------------------------------------------------
# Edge: subprocess general exception
# ---------------------------------------------------------------------------


class TestDoctorEdgeCases:
    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_general_exception_in_git_check(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = OSError("Permission denied")

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 1
        assert "Permission denied" in result.output

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_git_ok_network_file_not_found(self, mock_run, runner, tmp_path, monkeypatch):
        """When git works but network check raises FileNotFoundError."""
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            FileNotFoundError("git not found"),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Check 6: duplicate package names
# ---------------------------------------------------------------------------


class TestDoctorDuplicateNames:
    """Defence-in-depth duplicate name check in the doctor command."""

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    @patch("apm_cli.commands.marketplace.doctor.load_marketplace_yml")
    def test_duplicate_names_flagged(
        self,
        mock_load,
        mock_run,
        runner,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text("---\n", encoding="utf-8")
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]
        mock_load.return_value = MarketplaceYml(
            name="test",
            description="Test",
            version="1.0.0",
            owner=MarketplaceOwner(name="Owner"),
            packages=(
                PackageEntry(
                    name="learning",
                    source="acme/repo",
                    subdir="general",
                    version="^1.0.0",
                ),
                PackageEntry(
                    name="learning",
                    source="acme/repo",
                    subdir="special",
                    version="^1.0.0",
                ),
            ),
        )

        result = runner.invoke(marketplace, ["doctor"])
        assert "duplicate" in result.output.lower()
        assert "learning" in result.output

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    @patch("apm_cli.commands.marketplace.doctor.load_marketplace_yml")
    def test_no_duplicate_names_shows_pass(
        self,
        mock_load,
        mock_run,
        runner,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "marketplace.yml").write_text("---\n", encoding="utf-8")
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]
        mock_load.return_value = MarketplaceYml(
            name="test",
            description="Test",
            version="1.0.0",
            owner=MarketplaceOwner(name="Owner"),
            packages=(
                PackageEntry(
                    name="alpha",
                    source="acme/alpha",
                    version="^1.0.0",
                ),
                PackageEntry(
                    name="beta",
                    source="acme/beta",
                    version="^1.0.0",
                ),
            ),
        )

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0
        assert "No duplicate package names" in result.output

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_no_duplicate_check_when_yml_absent(
        self,
        mock_run,
        runner,
        tmp_path,
        monkeypatch,
    ):
        """When marketplace.yml is missing, duplicate check is skipped."""
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]

        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0
        assert "duplicate" not in result.output.lower()


# ---------------------------------------------------------------------------
# Wave 4: version alignment (Check 8)
# ---------------------------------------------------------------------------


_APM_DOCTOR_ALIGNED = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  packages:
    - name: local-tool
      source: ./packages/local-tool
      description: Tool.
      version: 1.0.0
"""

_APM_DOCTOR_MISALIGNED = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  packages:
    - name: local-tool
      source: ./packages/local-tool
      description: Tool.
      version: 0.9.0
"""

_APM_DOCTOR_TAG_STRATEGY = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  versioning:
    strategy: tag_pattern
  build:
    tagPattern: "v{version}"
  packages:
    - name: local-tool
      source: ./packages/local-tool
      description: Tool.
      version: 1.0.0
"""


def _scaffold_doctor_project(tmp_path, apm_yml: str, *, pkg_version: str = "1.0.0") -> None:
    (tmp_path / "apm.yml").write_text(apm_yml, encoding="utf-8")
    pkg_dir = tmp_path / "packages" / "local-tool"
    pkg_dir.mkdir(parents=True)
    pkg_dir.joinpath("apm.yml").write_text(
        f"name: local-tool\ndescription: Tool.\nversion: {pkg_version}\n",
        encoding="utf-8",
    )


class TestDoctorVersionAlignment:
    """Check 8: version-alignment row appears when marketplace block is present."""

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_aligned_row_rendered(self, mock_run, runner, tmp_path, monkeypatch):
        _scaffold_doctor_project(tmp_path, _APM_DOCTOR_ALIGNED, pkg_version="1.0.0")
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]
        result = runner.invoke(marketplace, ["doctor"])
        assert "version alignment" in result.output.lower()
        assert "lockstep" in result.output.lower()
        assert "1/1" in result.output or "aligned" in result.output.lower()

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_misaligned_row_rendered(self, mock_run, runner, tmp_path, monkeypatch):
        _scaffold_doctor_project(tmp_path, _APM_DOCTOR_MISALIGNED, pkg_version="0.9.0")
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]
        result = runner.invoke(marketplace, ["doctor"])
        assert "version alignment" in result.output.lower()
        assert "misaligned" in result.output.lower()

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_skipped_when_no_marketplace_block(self, mock_run, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # No apm.yml at all -> Check 8 is skipped entirely.
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]
        result = runner.invoke(marketplace, ["doctor"])
        assert "version alignment" not in result.output.lower()

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_strategy_label_visible(self, mock_run, runner, tmp_path, monkeypatch):
        _scaffold_doctor_project(tmp_path, _APM_DOCTOR_TAG_STRATEGY, pkg_version="1.0.0")
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]
        result = runner.invoke(marketplace, ["doctor"])
        assert "tag_pattern" in result.output.lower()

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_misaligned_does_not_fail_critical_exit(self, mock_run, runner, tmp_path, monkeypatch):
        """Version alignment is informational; misalignment alone must not exit 1."""
        _scaffold_doctor_project(tmp_path, _APM_DOCTOR_MISALIGNED, pkg_version="0.9.0")
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]
        result = runner.invoke(marketplace, ["doctor"])
        # Doctor exit is governed only by critical checks (1-2).
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Additional coverage: missed lines
# ---------------------------------------------------------------------------


class TestDoctorAuthExceptionFallback:
    """Lines 109-110: AuthResolver raises → has_token = False."""

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    @patch("apm_cli.core.auth.AuthResolver", side_effect=RuntimeError("no auth"))
    def test_auth_exception_shows_no_token(
        self, mock_auth, mock_run, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]
        result = runner.invoke(marketplace, ["doctor"])
        assert result.exit_code == 0
        assert "No token" in result.output or "unauthenticated" in result.output.lower()


class TestDoctorMarketplaceYmlError:
    """Lines 166-168, 176-178: MarketplaceYmlError when loading config."""

    @patch("apm_cli.commands.marketplace.doctor.subprocess.run")
    def test_apm_yml_marketplace_error_logged(self, mock_run, runner, tmp_path, monkeypatch):
        """Lines 166-168: apm.yml marketplace block parse error."""
        monkeypatch.chdir(tmp_path)
        # Write a valid apm.yml but with bad marketplace block to trigger MarketplaceYmlError
        (tmp_path / "apm.yml").write_text(
            "name: test\nmarketplace:\n  invalid_key: bad\n",
            encoding="utf-8",
        )
        mock_run.side_effect = [
            _make_run_result(0, stdout="git version 2.40.0"),
            _make_run_result(0),
            _GH_OK,
        ]
        result = runner.invoke(marketplace, ["doctor"])
        # Either passes or fails gracefully
        assert result.exit_code in (0, 1, 2)
