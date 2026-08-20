"""Unit tests for ``apm lifecycle`` CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import click.testing
import pytest
import yaml

from apm_cli.commands.lifecycle import _validate_script_file, lifecycle


@pytest.fixture()
def cli_runner():
    return click.testing.CliRunner()


def _write_yaml(path: Path, data: dict) -> Path:
    """Write a YAML file and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False), encoding="utf-8")
    return path


def _write_json(path: Path, data: dict) -> Path:
    """Write a JSON file and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestLifecycleList:
    def test_no_scripts_shows_info(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(lifecycle, [])
        assert result.exit_code == 0
        assert "No lifecycle scripts" in result.output

    def test_shows_discovered_scripts(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo hi"}]}},
        )
        result = cli_runner.invoke(lifecycle, [])
        assert result.exit_code == 0
        assert "1 script" in result.output


class TestLifecycleTest:
    def test_no_scripts_warns(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(lifecycle, ["test", "post-install"])
        assert result.exit_code == 0
        assert "No scripts registered" in result.output

    def test_dry_run_is_default(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo test-ok"}]}},
        )
        with patch("apm_cli.core.script_executors.subprocess.run") as mock_run:
            result = cli_runner.invoke(lifecycle, ["test", "post-install"])
        assert result.exit_code == 0
        assert "Dry-run" in result.output
        mock_run.assert_not_called()

    def test_fires_synthetic_event(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo test-ok"}]}},
        )
        with patch("apm_cli.core.script_executors.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="test-ok", stderr="", returncode=0)
            result = cli_runner.invoke(lifecycle, ["test", "post-install", "--execute"])
        assert result.exit_code == 0
        assert "fired" in result.output.lower()


class TestLifecycleInit:
    def test_requires_existing_apm_yml(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(lifecycle, ["init"])
        assert result.exit_code != 0
        assert "No apm.yml found" in result.output

    def test_injects_lifecycle_block(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yaml(tmp_path / "apm.yml", {"name": "demo"})
        result = cli_runner.invoke(lifecycle, ["init"])
        assert result.exit_code == 0
        data = yaml.safe_load((tmp_path / "apm.yml").read_text(encoding="utf-8"))
        assert "lifecycle" in data
        assert "post-install" in data["lifecycle"]

    def test_force_overwrites_existing_lifecycle(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yaml(tmp_path / "apm.yml", {"lifecycle": {"post-install": []}})
        result = cli_runner.invoke(lifecycle, ["init", "--force"])
        assert result.exit_code == 0
        data = yaml.safe_load((tmp_path / "apm.yml").read_text(encoding="utf-8"))
        assert data["lifecycle"]["post-install"]


class TestLifecycleValidate:
    def test_no_files_shows_info(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(lifecycle, ["validate"])
        assert result.exit_code == 0
        assert "No script files found" in result.output

    def test_valid_file_passes(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "bash": "echo ok"}]}},
        )
        result = cli_runner.invoke(lifecycle, ["validate"])
        assert result.exit_code == 0
        assert "valid" in result.output.lower()

    def test_invalid_yaml_fails(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(": - bad: yaml:\n  :", encoding="utf-8")
        result = cli_runner.invoke(lifecycle, ["validate"])
        assert result.exit_code != 0
        assert "Invalid YAML" in result.output or "YAML" in result.output

    def test_http_url_rejects_plain_http(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "http", "url": "http://bad"}]}},
        )
        result = cli_runner.invoke(lifecycle, ["validate"])
        assert result.exit_code != 0
        assert "https" in result.output.lower()


class TestValidateScriptFile:
    def test_missing_version_in_json(self, tmp_path):
        f = _write_json(tmp_path / "t.json", {"scripts": {}})
        errors = _validate_script_file(f, "policy")
        assert any("version" in e.lower() for e in errors)

    def test_missing_scripts_field_in_json(self, tmp_path):
        f = _write_json(tmp_path / "t.json", {"version": 1})
        errors = _validate_script_file(f, "policy")
        assert any("scripts" in e.lower() for e in errors)

    def test_valid_apm_yml_returns_empty(self, tmp_path):
        f = _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "run": "echo ok"}]}},
        )
        assert _validate_script_file(f, "project") == []


class TestLifecycleTrustUntrust:
    def test_trust_nonexistent_file(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(lifecycle, ["trust"])
        assert result.exit_code == 0
        assert "No apm.yml found" in result.output

    def test_trust_existing_file(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
        _write_yaml(tmp_path / "apm.yml", {"lifecycle": {"post-install": []}})
        result = cli_runner.invoke(lifecycle, ["trust"])
        assert result.exit_code == 0
        assert "Trusted" in result.output

    def test_untrust_when_not_trusted(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
        result = cli_runner.invoke(lifecycle, ["untrust"])
        assert result.exit_code == 0
        assert "nothing to revoke" in result.output.lower()


class TestLifecycleTestTrustDisplay:
    """Regression tests for trust-status display in 'apm lifecycle test' dry-run."""

    def test_dry_run_shows_trust_status_untrusted(self, cli_runner, tmp_path, monkeypatch):
        """Dry-run output must show [untrusted] when project lifecycle is not trusted.

        This guards the invariant that users can see whether --execute would
        actually fire scripts without first running 'apm lifecycle trust'.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
        _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "run": "echo ok"}]}},
        )
        result = cli_runner.invoke(lifecycle, ["test", "post-install"])
        assert result.exit_code == 0
        assert "Dry-run" in result.output
        assert "untrusted" in result.output.lower()

    def test_dry_run_shows_trust_status_trusted(self, cli_runner, tmp_path, monkeypatch):
        """Dry-run output must show [trusted] after 'apm lifecycle trust' has been run."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
        _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "run": "echo ok"}]}},
        )
        # Trust the lifecycle block first.
        trust_result = cli_runner.invoke(lifecycle, ["trust"])
        assert trust_result.exit_code == 0

        result = cli_runner.invoke(lifecycle, ["test", "post-install"])
        assert result.exit_code == 0
        assert "Dry-run" in result.output
        assert "trusted" in result.output.lower()

    def test_execute_flag_runs_scripts_regardless_of_trust(self, cli_runner, tmp_path, monkeypatch):
        """Document intentional design: --execute fires scripts even when untrusted.

        'apm lifecycle test --execute' is an explicit developer action (scaffold +
        verify wiring before first trust). It bypasses the trust gate by design so
        authors can iterate without cycling trust on every script edit. This is
        distinct from 'apm install' which ALWAYS requires trust.

        This test is a regression trap: if the behavior changes (either to enforce
        trust or to change the bypass semantics), this test must be updated with a
        comment explaining the new design rationale.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
        _write_yaml(
            tmp_path / "apm.yml",
            {"lifecycle": {"post-install": [{"type": "command", "run": "echo ok"}]}},
        )
        # Deliberately do NOT run 'apm lifecycle trust' before --execute.
        with patch("apm_cli.core.script_executors.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
            result = cli_runner.invoke(lifecycle, ["test", "post-install", "--execute"])
        assert result.exit_code == 0
        assert "fired" in result.output.lower()
