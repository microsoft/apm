"""``apm preview`` hands off a run command that keeps its params (#3994)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = pytest.mark.component


def test_preview_run_hint_repeats_params(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text(
        'name: pv\nversion: 1.0.0\nscripts:\n  start: "echo hi"\n', encoding="utf-8"
    )
    result = CliRunner().invoke(cli, ["preview", "start", "--param", "name=Alice Smith"])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "Execute with: apm run start --param 'name=Alice Smith'" in flat


def test_run_reports_success_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text(
        'name: pv\nversion: 1.0.0\nscripts:\n  start: "echo hi"\n', encoding="utf-8"
    )
    result = CliRunner().invoke(cli, ["run", "start"])
    assert result.exit_code == 0, result.output
    assert result.output.count("success") == 1, result.output
