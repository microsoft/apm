"""Tests for ``apm marketplace plugin {add,set,remove}`` CLI commands."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yml(tmp_path: Path, content: str | None = None) -> Path:
    """Scaffold a valid ``marketplace.yml`` in *tmp_path*."""
    if content is None:
        content = textwrap.dedent("""\
            name: test-marketplace
            description: Test marketplace
            version: 1.0.0
            owner:
              name: Test Owner
            packages:
              - name: existing-package
                source: acme/existing-package
                version: ">=1.0.0"
                description: An existing package
        """)
    p = tmp_path / "marketplace.yml"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# plugin add
# ---------------------------------------------------------------------------


class TestPluginAdd:
    def test_happy_path_no_verify(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "plugin",
                "add",
                "acme/new-tool",
                "--version",
                ">=2.0.0",
                "--no-verify",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "new-tool" in result.output

    def test_duplicate_name_exits_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "plugin",
                "add",
                "acme/existing-package",
                "--version",
                ">=1.0.0",
                "--no-verify",
            ],
        )
        assert result.exit_code == 2
        assert "already exists" in result.output

    def test_missing_version_and_ref_exits_2(
        self, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["plugin", "add", "acme/tool", "--no-verify"],
        )
        assert result.exit_code == 2
        assert "At least one" in result.output

    def test_version_and_ref_conflict_exits_2(
        self, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "plugin",
                "add",
                "acme/tool",
                "--version",
                ">=1.0.0",
                "--ref",
                "abc",
                "--no-verify",
            ],
        )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output.lower()

    def test_help_renders(self, runner):
        result = runner.invoke(marketplace, ["plugin", "add", "--help"])
        assert result.exit_code == 0
        assert "Add a plugin" in result.output

    def test_verify_calls_ref_resolver(
        self, runner, tmp_path, monkeypatch
    ):
        """Without --no-verify the command calls list_remote_refs."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        monkeypatch.setattr(
            "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
            lambda self, source: [],
        )
        result = runner.invoke(
            marketplace,
            [
                "plugin",
                "add",
                "acme/verified-tool",
                "--version",
                ">=1.0.0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "verified-tool" in result.output


# ---------------------------------------------------------------------------
# plugin set
# ---------------------------------------------------------------------------


class TestPluginSet:
    def test_happy_path_update_version(
        self, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "plugin",
                "set",
                "existing-package",
                "--version",
                ">=2.0.0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Updated" in result.output

    def test_package_not_found_exits_2(
        self, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "plugin",
                "set",
                "nonexistent",
                "--version",
                ">=1.0.0",
            ],
        )
        assert result.exit_code == 2
        assert "not found" in result.output

    def test_help_renders(self, runner):
        result = runner.invoke(marketplace, ["plugin", "set", "--help"])
        assert result.exit_code == 0
        assert "Update a plugin" in result.output

    def test_set_no_fields_errors(self, runner, tmp_path, monkeypatch):
        """Calling ``plugin set`` with no field flags produces an error."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["plugin", "set", "existing-package"],
        )
        assert result.exit_code == 1
        assert "No fields specified" in result.output


# ---------------------------------------------------------------------------
# plugin remove
# ---------------------------------------------------------------------------


class TestPluginRemove:
    def test_happy_path_with_yes(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["plugin", "remove", "existing-package", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert "Removed" in result.output

    def test_without_yes_non_interactive_cancels(
        self, runner, tmp_path, monkeypatch
    ):
        """Non-interactive mode (CliRunner has no TTY) cancels gracefully."""
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["plugin", "remove", "existing-package"],
        )
        # click.confirm raises Abort when stdin is not a TTY;
        # the command catches it and prints "Cancelled.".
        assert result.exit_code == 0
        assert "Cancelled." in result.output

    def test_package_not_found_exits_2(
        self, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["plugin", "remove", "nonexistent", "--yes"],
        )
        assert result.exit_code == 2
        assert "not found" in result.output

    def test_help_renders(self, runner):
        result = runner.invoke(marketplace, ["plugin", "remove", "--help"])
        assert result.exit_code == 0
        assert "Remove a plugin" in result.output


# ---------------------------------------------------------------------------
# UX4: --version/--ref mutual exclusivity in plugin add
# ---------------------------------------------------------------------------


class TestPluginAddMutualExclusivity:
    """The ``add`` command must reject ``--version`` and ``--ref`` together."""

    def test_version_and_ref_mutually_exclusive(
        self, runner, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "plugin",
                "add",
                "acme/new-tool",
                "--version",
                "1.0.0",
                "--ref",
                "main",
                "--no-verify",
            ],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()
