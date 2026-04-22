"""Tests for ``apm marketplace init`` subcommand."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace
from apm_cli.marketplace.yml_schema import load_marketplace_yml


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestInitHappyPath:
    def test_creates_marketplace_yml(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0
        assert (tmp_path / "marketplace.yml").exists()

    def test_success_message(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0
        assert "Created marketplace.yml" in result.output

    def test_next_steps_shown(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0
        assert "apm marketplace build" in result.output

    def test_template_roundtrips_through_schema(self, runner, tmp_path, monkeypatch):
        """The scaffolded file must parse without errors."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0
        yml = load_marketplace_yml(tmp_path / "marketplace.yml")
        assert yml.name == "my-marketplace"
        assert yml.version == "0.1.0"
        assert yml.owner.name == "acme-org"
        assert len(yml.packages) >= 1


# ---------------------------------------------------------------------------
# File-already-exists guard
# ---------------------------------------------------------------------------


class TestInitExistsGuard:
    def test_error_when_file_exists(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        existing = tmp_path / "marketplace.yml"
        existing.write_text("name: keep-me\n", encoding="utf-8")

        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_file_unchanged_without_force(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        existing = tmp_path / "marketplace.yml"
        original_content = "name: keep-me\n"
        existing.write_text(original_content, encoding="utf-8")

        runner.invoke(marketplace, ["init"])
        assert existing.read_text(encoding="utf-8") == original_content

    def test_force_overwrites(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        existing = tmp_path / "marketplace.yml"
        existing.write_text("name: stale-sentinel\n", encoding="utf-8")

        result = runner.invoke(marketplace, ["init", "--force"])
        assert result.exit_code == 0
        new_content = existing.read_text(encoding="utf-8")
        assert "my-marketplace" in new_content
        assert "stale-sentinel" not in new_content


# ---------------------------------------------------------------------------
# .gitignore staleness check
# ---------------------------------------------------------------------------


class TestInitGitignoreCheck:
    def test_warns_when_gitignore_ignores_marketplace_json(
        self, runner, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gitignore").write_text(
            "marketplace.json\n", encoding="utf-8",
        )
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0
        assert ".gitignore ignores marketplace.json" in result.output

    def test_warns_for_glob_pattern(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gitignore").write_text(
            "**/marketplace.json\n", encoding="utf-8",
        )
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0
        assert ".gitignore ignores marketplace.json" in result.output

    def test_warns_for_rooted_pattern(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gitignore").write_text(
            "/marketplace.json\n", encoding="utf-8",
        )
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0
        assert ".gitignore ignores marketplace.json" in result.output

    def test_no_warning_for_commented_line(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gitignore").write_text(
            "# marketplace.json\n", encoding="utf-8",
        )
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0
        assert ".gitignore ignores marketplace.json" not in result.output

    def test_no_gitignore_check_suppresses_warning(
        self, runner, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gitignore").write_text(
            "marketplace.json\n", encoding="utf-8",
        )
        result = runner.invoke(marketplace, ["init", "--no-gitignore-check"])
        assert result.exit_code == 0
        assert ".gitignore ignores marketplace.json" not in result.output

    def test_no_warning_without_gitignore(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0
        assert ".gitignore" not in result.output


# ---------------------------------------------------------------------------
# --verbose flag
# ---------------------------------------------------------------------------


class TestInitVerbose:
    def test_verbose_shows_path(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init", "--verbose"])
        assert result.exit_code == 0
        assert "Path:" in result.output


# ---------------------------------------------------------------------------
# Content checks
# ---------------------------------------------------------------------------


class TestInitContentSafety:
    def test_template_contains_acme_org(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner.invoke(marketplace, ["init"])
        content = (tmp_path / "marketplace.yml").read_text(encoding="utf-8")
        assert "acme-org" in content

    def test_template_has_no_epam_references(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner.invoke(marketplace, ["init"])
        content = (tmp_path / "marketplace.yml").read_text(encoding="utf-8").lower()
        assert "epam" not in content
        assert "bookstore" not in content
        assert "agent-forge" not in content

    def test_template_is_pure_ascii(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner.invoke(marketplace, ["init"])
        content = (tmp_path / "marketplace.yml").read_text(encoding="utf-8")
        content.encode("ascii")  # raises UnicodeEncodeError if non-ASCII


# ---------------------------------------------------------------------------
# --name / --owner flags
# ---------------------------------------------------------------------------


class TestInitNameOwnerFlags:
    def test_custom_name(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init", "--name", "cool-tools"])
        assert result.exit_code == 0
        yml = load_marketplace_yml(tmp_path / "marketplace.yml")
        assert yml.name == "cool-tools"

    def test_custom_owner(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init", "--owner", "my-org"])
        assert result.exit_code == 0
        yml = load_marketplace_yml(tmp_path / "marketplace.yml")
        assert yml.owner.name == "my-org"

    def test_custom_name_and_owner(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            marketplace, ["init", "--name", "my-mkt", "--owner", "my-team"],
        )
        assert result.exit_code == 0
        yml = load_marketplace_yml(tmp_path / "marketplace.yml")
        assert yml.name == "my-mkt"
        assert yml.owner.name == "my-team"
        content = (tmp_path / "marketplace.yml").read_text(encoding="utf-8")
        assert "my-team" in content
        # The default acme-org should not appear when owner is overridden.
        assert "acme-org" not in content

    def test_defaults_without_flags(self, runner, tmp_path, monkeypatch):
        """Without --name/--owner the defaults are used."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0
        yml = load_marketplace_yml(tmp_path / "marketplace.yml")
        assert yml.name == "my-marketplace"
        assert yml.owner.name == "acme-org"

    def test_custom_values_are_pure_ascii(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            marketplace, ["init", "--name", "ascii-only", "--owner", "plain-org"],
        )
        assert result.exit_code == 0
        content = (tmp_path / "marketplace.yml").read_text(encoding="utf-8")
        content.encode("ascii")  # raises UnicodeEncodeError if non-ASCII
