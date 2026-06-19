"""Tests for the top-level ``apm doctor`` command and its deprecated alias."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

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


def test_marketplace_doctor_not_available():
    """``apm marketplace doctor`` must not be a registered subcommand."""
    runner = CliRunner()
    result = runner.invoke(cli, ["marketplace", "--help"])
    assert result.exit_code == 0
    assert "doctor  " not in result.output  # column-aligned listing


def test_apm_doctor_runs_diagnostics(mock_subprocess_success):
    """Top-level invocation should produce the diagnostics table."""
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])
    # Network check may legitimately fail in sandboxed test env -> non-zero ok.
    assert result.exit_code in (0, 1)
    assert "git" in result.output.lower()
