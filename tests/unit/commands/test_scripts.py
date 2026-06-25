"""Unit tests for ``apm scripts`` CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import click.testing
import pytest

from apm_cli.commands.scripts import _validate_script_file, scripts


@pytest.fixture()
def cli_runner():
    return click.testing.CliRunner()


def _write_script_file(path: Path, data: dict) -> Path:
    """Write a script JSON file and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# -- apm scripts (list) ----------------------------------------------------


class TestScriptsList:
    def test_no_scripts_shows_info(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(scripts, [])
        assert result.exit_code == 0
        assert "No lifecycle scripts" in result.output

    def test_shows_discovered_scripts(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_script_file(
            tmp_path / ".apm" / "scripts.json",
            {
                "version": 1,
                "scripts": {
                    "post-install": [{"type": "command", "bash": "echo hi"}],
                },
            },
        )
        result = cli_runner.invoke(scripts, [])
        assert result.exit_code == 0
        assert "1 script" in result.output


# -- apm scripts test ------------------------------------------------------


class TestScriptsTest:
    def test_no_scripts_warns(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(scripts, ["test", "post-install"])
        assert result.exit_code == 0
        assert "No scripts registered" in result.output

    def test_dry_run_is_default(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
        _write_script_file(
            tmp_path / ".apm" / "scripts.json",
            {
                "version": 1,
                "scripts": {
                    "post-install": [{"type": "command", "bash": "echo test-ok"}],
                },
            },
        )
        with patch("apm_cli.core.script_executors.subprocess.run") as mock_run:
            result = cli_runner.invoke(scripts, ["test", "post-install"])
        assert result.exit_code == 0
        assert "Dry-run" in result.output
        mock_run.assert_not_called()

    def test_fires_synthetic_event(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
        _write_script_file(
            tmp_path / ".apm" / "scripts.json",
            {
                "version": 1,
                "scripts": {
                    "post-install": [{"type": "command", "bash": "echo test-ok"}],
                },
            },
        )
        with patch("apm_cli.core.script_executors.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="test-ok", stderr="", returncode=0)
            result = cli_runner.invoke(scripts, ["test", "post-install", "--execute"])
        assert result.exit_code == 0
        assert "fired" in result.output.lower() or "Fired" in result.output

    def test_accepts_all_event_names(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        events = [
            "pre-install",
            "post-install",
            "pre-update",
            "post-update",
            "pre-uninstall",
            "post-uninstall",
        ]
        for event in events:
            result = cli_runner.invoke(scripts, ["test", event])
            assert result.exit_code == 0

    def test_rejects_invalid_event(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(scripts, ["test", "invalid-event"])
        assert result.exit_code != 0

    def test_default_event_is_post_install(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(scripts, ["test"])
        assert result.exit_code == 0
        # Either shows "No scripts" or fires -- both are valid for post-install default


# -- apm scripts init ------------------------------------------------------


class TestScriptsInit:
    def test_creates_script_file(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(scripts, ["init"])
        assert result.exit_code == 0
        assert "Created script file" in result.output
        script_file = tmp_path / ".apm" / "scripts.json"
        assert script_file.exists()
        data = json.loads(script_file.read_text())
        assert data["version"] == 1
        assert "post-install" in data["scripts"]

    def test_refuses_overwrite_without_force(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_script_file(
            tmp_path / ".apm" / "scripts.json",
            {"version": 1, "scripts": {}},
        )
        result = cli_runner.invoke(scripts, ["init"])
        assert result.exit_code == 0
        assert "already exists" in result.output

    def test_force_overwrites(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_script_file(
            tmp_path / ".apm" / "scripts.json",
            {"version": 1, "scripts": {}},
        )
        result = cli_runner.invoke(scripts, ["init", "--force"])
        assert result.exit_code == 0
        assert "Created script file" in result.output
        data = json.loads((tmp_path / ".apm" / "scripts.json").read_text())
        assert "post-install" in data["scripts"]


# -- apm scripts validate --------------------------------------------------


class TestScriptsValidate:
    def test_no_files_shows_info(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(scripts, ["validate"])
        assert result.exit_code == 0
        assert "No script files found" in result.output

    def test_valid_file_passes(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_script_file(
            tmp_path / ".apm" / "scripts.json",
            {
                "version": 1,
                "scripts": {
                    "post-install": [{"type": "command", "bash": "echo ok"}],
                },
            },
        )
        result = cli_runner.invoke(scripts, ["validate"])
        assert result.exit_code == 0
        assert "valid" in result.output.lower()

    def test_invalid_json_fails(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir(parents=True)
        (apm_dir / "scripts.json").write_text("{not valid json", encoding="utf-8")
        result = cli_runner.invoke(scripts, ["validate"])
        assert result.exit_code != 0
        assert "Invalid JSON" in result.output

    def test_wrong_version_fails(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_script_file(
            tmp_path / ".apm" / "scripts.json",
            {"version": 99, "scripts": {}},
        )
        result = cli_runner.invoke(scripts, ["validate"])
        assert result.exit_code != 0
        assert "Unsupported version" in result.output

    def test_unknown_event_fails(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_script_file(
            tmp_path / ".apm" / "scripts.json",
            {
                "version": 1,
                "scripts": {"on-banana": [{"type": "command", "bash": "echo"}]},
            },
        )
        result = cli_runner.invoke(scripts, ["validate"])
        assert result.exit_code != 0
        assert "Unknown event" in result.output

    def test_command_without_bash_or_command_fails(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_script_file(
            tmp_path / ".apm" / "scripts.json",
            {
                "version": 1,
                "scripts": {"post-install": [{"type": "command"}]},
            },
        )
        result = cli_runner.invoke(scripts, ["validate"])
        assert result.exit_code != 0
        assert "bash" in result.output or "command" in result.output

    def test_http_without_url_fails(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_script_file(
            tmp_path / ".apm" / "scripts.json",
            {
                "version": 1,
                "scripts": {"post-install": [{"type": "http"}]},
            },
        )
        result = cli_runner.invoke(scripts, ["validate"])
        assert result.exit_code != 0
        assert "url" in result.output.lower()

    def test_http_url_rejects_plain_http(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_script_file(
            tmp_path / ".apm" / "scripts.json",
            {
                "version": 1,
                "scripts": {
                    "post-install": [{"type": "http", "url": "http://insecure.com/script"}]
                },
            },
        )
        result = cli_runner.invoke(scripts, ["validate"])
        assert result.exit_code != 0
        assert "https" in result.output.lower()

    def test_multiple_errors_reported(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_script_file(
            tmp_path / ".apm" / "scripts.json",
            {
                "version": 1,
                "scripts": {
                    "post-install": [{"type": "command"}, {"type": "http"}],
                },
            },
        )
        result = cli_runner.invoke(scripts, ["validate"])
        assert result.exit_code != 0


# -- _validate_script_file (unit) ------------------------------------------


class TestValidateScriptFile:
    def test_missing_version(self, tmp_path):
        f = _write_script_file(tmp_path / "t.json", {"scripts": {}})
        errors = _validate_script_file(f, "project")
        assert any("version" in e.lower() for e in errors)

    def test_missing_scripts_field(self, tmp_path):
        f = _write_script_file(tmp_path / "t.json", {"version": 1})
        errors = _validate_script_file(f, "project")
        assert any("scripts" in e.lower() for e in errors)

    def test_non_dict_root(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_text("[]", encoding="utf-8")
        errors = _validate_script_file(path, "project")
        assert any("object" in e.lower() for e in errors)

    def test_unreadable_file(self, tmp_path):
        path = tmp_path / "missing.json"
        errors = _validate_script_file(path, "project")
        assert any("read" in e.lower() for e in errors)

    def test_valid_file_returns_empty(self, tmp_path):
        f = _write_script_file(
            tmp_path / "t.json",
            {
                "version": 1,
                "scripts": {
                    "post-install": [{"type": "command", "bash": "echo"}],
                    "pre-uninstall": [{"type": "http", "url": "https://example.com/script"}],
                },
            },
        )
        errors = _validate_script_file(f, "project")
        assert errors == []

    def test_unknown_script_type(self, tmp_path):
        f = _write_script_file(
            tmp_path / "t.json",
            {
                "version": 1,
                "scripts": {"post-install": [{"type": "magic"}]},
            },
        )
        errors = _validate_script_file(f, "project")
        assert any("unknown type" in e.lower() for e in errors)


# -- apm scripts trust / untrust -------------------------------------------


class TestScriptsTrustUntrust:
    def test_trust_nonexistent_file(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(scripts, ["trust"])
        assert result.exit_code == 0
        assert "No project scripts file" in result.output

    def test_trust_existing_file(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
        _write_script_file(
            tmp_path / ".apm" / "scripts.json",
            {"version": 1, "scripts": {}},
        )
        result = cli_runner.invoke(scripts, ["trust"])
        assert result.exit_code == 0
        assert "Trusted" in result.output

    def test_untrust_when_not_trusted(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
        result = cli_runner.invoke(scripts, ["untrust"])
        assert result.exit_code == 0
        assert (
            "not trusted" in result.output.lower() or "nothing to revoke" in result.output.lower()
        )
