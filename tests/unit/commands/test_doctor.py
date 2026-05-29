"""Tests for the top-level ``apm doctor`` command and its deprecated alias."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.commands.marketplace import marketplace

# Token env vars that AuthResolver inspects.  Cleared so the doctor's auth
# check is deterministic regardless of the host environment.
_TOKEN_ENV_VARS = ("GITHUB_APM_PAT", "GITHUB_TOKEN", "GH_TOKEN")


@pytest.fixture(autouse=True)
def _clear_token_env(monkeypatch):
    for var in _TOKEN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def mock_subprocess_success():
    """Stub git/gh subprocess calls to deterministic success."""
    with patch("apm_cli.commands.marketplace.doctor.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "git version 2.42.0"
        run.return_value.stderr = ""
        yield run


def test_apm_doctor_registered_at_top_level():
    """`apm doctor --help` must succeed -- it is the discoverability fix."""
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "environment diagnostics" in result.output.lower()


def test_apm_doctor_appears_in_root_help():
    """`apm --help` must list `doctor` so users can discover it."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.output


def test_common_workflows_footer_present():
    """`apm --help` epilog must surface the common-workflows hint."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Common workflows" in result.output
    assert "apm install --frozen" in result.output
    assert "apm doctor" in result.output


def test_marketplace_doctor_hidden_from_help():
    """Legacy `apm marketplace doctor` must not appear in marketplace --help."""
    runner = CliRunner()
    result = runner.invoke(cli, ["marketplace", "--help"])
    assert result.exit_code == 0
    # 'doctor' as a subcommand listing should be gone from the Authoring
    # commands block now that it has been promoted to top-level.
    assert "doctor  " not in result.output  # column-aligned listing


def test_marketplace_doctor_still_works_with_deprecation_hint(mock_subprocess_success):
    """Legacy invocation must keep working and print the migration hint."""
    runner = CliRunner()
    result = runner.invoke(marketplace, ["doctor"])
    # The deprecation hint is emitted with err=True. Click 8.2 separates
    # stdout/stderr by default, so check both to stay version-agnostic.
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "deprecated" in combined.lower()
    assert "apm doctor" in combined
    # And the diagnostics still run.
    assert result.exit_code in (0, 1)  # 1 if network unreachable in sandbox


def test_apm_doctor_runs_diagnostics(mock_subprocess_success):
    """Top-level invocation should produce the diagnostics table."""
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])
    # Network check may legitimately fail in sandboxed test env -> non-zero ok.
    assert result.exit_code in (0, 1)
    assert "git" in result.output.lower()
