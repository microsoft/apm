"""Unit tests for the root-level ``--quiet`` / ``-q`` flag."""

from __future__ import annotations

import os
from unittest.mock import patch

import click
from click.testing import CliRunner

from apm_cli.cli import cli


def test_quiet_flag_sets_apm_progress_env(monkeypatch):
    """``--quiet`` must set APM_PROGRESS=quiet as documented."""
    monkeypatch.delenv("APM_PROGRESS", raising=False)

    ctx = click.Context(cli, info_name="apm")
    ctx.resilient_parsing = True
    with ctx:
        cli.callback(verbose=False, quiet=True)
        assert os.environ.get("APM_PROGRESS") == "quiet"


def test_default_invocation_does_not_set_apm_progress(monkeypatch):
    """Default behaviour must not touch APM_PROGRESS."""
    monkeypatch.delenv("APM_PROGRESS", raising=False)

    ctx = click.Context(cli, info_name="apm")
    ctx.resilient_parsing = True
    with ctx:
        cli.callback(verbose=False, quiet=False)
        assert "APM_PROGRESS" not in os.environ


def test_default_invocation_preserves_existing_apm_progress(monkeypatch):
    monkeypatch.setenv("APM_PROGRESS", "always")

    ctx = click.Context(cli, info_name="apm")
    ctx.resilient_parsing = True
    with ctx:
        cli.callback(verbose=False, quiet=False)
        assert os.environ.get("APM_PROGRESS") == "always"


def test_quiet_and_verbose_are_mutually_exclusive():
    runner = CliRunner()
    result = runner.invoke(cli, ["--quiet", "--verbose", "doctor"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_quiet_short_alias_sets_apm_progress(monkeypatch):
    monkeypatch.delenv("APM_PROGRESS", raising=False)
    seen: dict[str, str | None] = {}

    def _capture_progress(*_args, **_kwargs):
        seen["APM_PROGRESS"] = os.environ.get("APM_PROGRESS")

    with (
        patch("apm_cli.cli._check_and_notify_updates"),
        patch("apm_cli.commands.config._show_all_user_config", side_effect=_capture_progress),
    ):
        result = CliRunner().invoke(cli, ["-q", "config", "list"])

    assert result.exit_code == 0
    assert seen.get("APM_PROGRESS") == "quiet"


@patch("apm_cli.commands.install._validate_package_exists", return_value=False)
def test_quiet_still_surfaces_validation_errors(mock_validate):
    """Errors and warnings must remain visible under ``--quiet``."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["--quiet", "install", "owner/nonexistent"])

    assert result.exit_code != 0
    assert mock_validate.called
    output = result.output.lower()
    assert "error" in output or "owner/nonexistent" in output


def test_root_help_lists_quiet_flag():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "--quiet" in result.output or "-q" in result.output
