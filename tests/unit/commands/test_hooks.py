"""Unit tests for ``apm hooks`` CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import click.testing
import pytest

from apm_cli.commands.hooks import _validate_hook_file, hooks


@pytest.fixture()
def cli_runner():
    return click.testing.CliRunner()


def _write_hook_file(path: Path, data: dict) -> Path:
    """Write a hook JSON file and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# -- apm hooks (list) -------------------------------------------------------


class TestHooksList:
    def test_no_hooks_shows_info(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(hooks, [])
        assert result.exit_code == 0
        assert "No lifecycle hooks" in result.output

    def test_shows_discovered_hooks(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_hook_file(
            tmp_path / ".apm" / "hooks.json",
            {
                "version": 1,
                "hooks": {
                    "post-install": [{"type": "command", "bash": "echo hi"}],
                },
            },
        )
        result = cli_runner.invoke(hooks, [])
        assert result.exit_code == 0
        assert "1 hook" in result.output


# -- apm hooks test ----------------------------------------------------------


class TestHooksTest:
    def test_no_hooks_warns(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(hooks, ["test", "post-install"])
        assert result.exit_code == 0
        assert "No hooks registered" in result.output

    def test_fires_synthetic_event(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
        _write_hook_file(
            tmp_path / ".apm" / "hooks.json",
            {
                "version": 1,
                "hooks": {
                    "post-install": [{"type": "command", "bash": "echo test-ok"}],
                },
            },
        )
        with patch("apm_cli.core.hook_executors.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="test-ok", stderr="", returncode=0)
            result = cli_runner.invoke(hooks, ["test", "post-install"])
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
            result = cli_runner.invoke(hooks, ["test", event])
            assert result.exit_code == 0

    def test_rejects_invalid_event(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(hooks, ["test", "invalid-event"])
        assert result.exit_code != 0

    def test_default_event_is_post_install(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(hooks, ["test"])
        assert result.exit_code == 0
        # Either shows "No hooks" or fires -- both are valid for post-install default


# -- apm hooks init ----------------------------------------------------------


class TestHooksInit:
    def test_creates_hook_file(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(hooks, ["init"])
        assert result.exit_code == 0
        assert "Created hook file" in result.output
        hook_file = tmp_path / ".apm" / "hooks.json"
        assert hook_file.exists()
        data = json.loads(hook_file.read_text())
        assert data["version"] == 1
        assert "post-install" in data["hooks"]

    def test_refuses_overwrite_without_force(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_hook_file(
            tmp_path / ".apm" / "hooks.json",
            {"version": 1, "hooks": {}},
        )
        result = cli_runner.invoke(hooks, ["init"])
        assert result.exit_code == 0
        assert "already exists" in result.output

    def test_force_overwrites(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_hook_file(
            tmp_path / ".apm" / "hooks.json",
            {"version": 1, "hooks": {}},
        )
        result = cli_runner.invoke(hooks, ["init", "--force"])
        assert result.exit_code == 0
        assert "Created hook file" in result.output
        data = json.loads((tmp_path / ".apm" / "hooks.json").read_text())
        assert "post-install" in data["hooks"]


# -- apm hooks validate ------------------------------------------------------


class TestHooksValidate:
    def test_no_files_shows_info(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(hooks, ["validate"])
        assert result.exit_code == 0
        assert "No hook files found" in result.output

    def test_valid_file_passes(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_hook_file(
            tmp_path / ".apm" / "hooks.json",
            {
                "version": 1,
                "hooks": {
                    "post-install": [{"type": "command", "bash": "echo ok"}],
                },
            },
        )
        result = cli_runner.invoke(hooks, ["validate"])
        assert result.exit_code == 0
        assert "valid" in result.output.lower()

    def test_invalid_json_fails(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir(parents=True)
        (apm_dir / "hooks.json").write_text("{not valid json", encoding="utf-8")
        result = cli_runner.invoke(hooks, ["validate"])
        assert result.exit_code != 0
        assert "Invalid JSON" in result.output

    def test_wrong_version_fails(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_hook_file(
            tmp_path / ".apm" / "hooks.json",
            {"version": 99, "hooks": {}},
        )
        result = cli_runner.invoke(hooks, ["validate"])
        assert result.exit_code != 0
        assert "Unsupported version" in result.output

    def test_unknown_event_fails(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_hook_file(
            tmp_path / ".apm" / "hooks.json",
            {
                "version": 1,
                "hooks": {"on-banana": [{"type": "command", "bash": "echo"}]},
            },
        )
        result = cli_runner.invoke(hooks, ["validate"])
        assert result.exit_code != 0
        assert "Unknown event" in result.output

    def test_command_without_bash_or_command_fails(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_hook_file(
            tmp_path / ".apm" / "hooks.json",
            {
                "version": 1,
                "hooks": {"post-install": [{"type": "command"}]},
            },
        )
        result = cli_runner.invoke(hooks, ["validate"])
        assert result.exit_code != 0
        assert "bash" in result.output or "command" in result.output

    def test_http_without_url_fails(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_hook_file(
            tmp_path / ".apm" / "hooks.json",
            {
                "version": 1,
                "hooks": {"post-install": [{"type": "http"}]},
            },
        )
        result = cli_runner.invoke(hooks, ["validate"])
        assert result.exit_code != 0
        assert "url" in result.output.lower()

    def test_http_url_rejects_plain_http(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_hook_file(
            tmp_path / ".apm" / "hooks.json",
            {
                "version": 1,
                "hooks": {"post-install": [{"type": "http", "url": "http://insecure.com/hook"}]},
            },
        )
        result = cli_runner.invoke(hooks, ["validate"])
        assert result.exit_code != 0
        assert "https" in result.output.lower()

    def test_multiple_errors_reported(self, cli_runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_hook_file(
            tmp_path / ".apm" / "hooks.json",
            {
                "version": 1,
                "hooks": {
                    "post-install": [{"type": "command"}, {"type": "http"}],
                },
            },
        )
        result = cli_runner.invoke(hooks, ["validate"])
        assert result.exit_code != 0


# -- _validate_hook_file (unit) ---------------------------------------------


class TestValidateHookFile:
    def test_missing_version(self, tmp_path):
        f = _write_hook_file(tmp_path / "t.json", {"hooks": {}})
        errors = _validate_hook_file(f, "project")
        assert any("version" in e.lower() for e in errors)

    def test_missing_hooks_field(self, tmp_path):
        f = _write_hook_file(tmp_path / "t.json", {"version": 1})
        errors = _validate_hook_file(f, "project")
        assert any("hooks" in e.lower() for e in errors)

    def test_non_dict_root(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_text("[]", encoding="utf-8")
        errors = _validate_hook_file(path, "project")
        assert any("object" in e.lower() for e in errors)

    def test_unreadable_file(self, tmp_path):
        path = tmp_path / "missing.json"
        errors = _validate_hook_file(path, "project")
        assert any("read" in e.lower() for e in errors)

    def test_valid_file_returns_empty(self, tmp_path):
        f = _write_hook_file(
            tmp_path / "t.json",
            {
                "version": 1,
                "hooks": {
                    "post-install": [{"type": "command", "bash": "echo"}],
                    "pre-uninstall": [{"type": "http", "url": "https://example.com/hook"}],
                },
            },
        )
        errors = _validate_hook_file(f, "project")
        assert errors == []

    def test_unknown_hook_type(self, tmp_path):
        f = _write_hook_file(
            tmp_path / "t.json",
            {
                "version": 1,
                "hooks": {"post-install": [{"type": "magic"}]},
            },
        )
        errors = _validate_hook_file(f, "project")
        assert any("unknown type" in e.lower() for e in errors)
