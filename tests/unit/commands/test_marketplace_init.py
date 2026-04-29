"""Tests for ``apm marketplace init`` subcommand (post-fold).

After the fold (#1036), ``apm marketplace init`` writes a ``marketplace:``
block into ``apm.yml`` (scaffolding ``apm.yml`` if absent). It no longer
creates standalone ``marketplace.yml`` files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace


@pytest.fixture
def runner():
    return CliRunner()


def _load_marketplace_block(apm_yml_path: Path) -> dict:
    data = yaml.safe_load(apm_yml_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "marketplace" in data
    return data["marketplace"]


# ---------------------------------------------------------------------------
# Happy path: scaffolds apm.yml + marketplace: block
# ---------------------------------------------------------------------------


class TestInitHappyPath:
    def test_creates_apm_yml_when_absent(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "apm.yml").exists()
        # No legacy marketplace.yml is created.
        assert not (tmp_path / "marketplace.yml").exists()

    def test_injects_marketplace_block_into_existing_apm_yml(
        self, runner, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            "name: my-app\nversion: 1.0.0\ndescription: existing\n",
            encoding="utf-8",
        )
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0, result.output
        block = _load_marketplace_block(tmp_path / "apm.yml")
        assert "owner" in block
        assert "packages" in block

    def test_success_message(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0
        # Single collapsed success line for scaffold-and-inject path.
        assert "Created apm.yml with 'marketplace:' block" in result.output

    def test_next_steps_shown(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0
        assert "apm pack" in result.output


# ---------------------------------------------------------------------------
# Existing-block guard
# ---------------------------------------------------------------------------


class TestInitExistsGuard:
    def test_error_when_marketplace_block_exists(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            "name: my-app\nversion: 1.0.0\ndescription: x\n"
            "marketplace:\n  owner:\n    name: keep-me\n",
            encoding="utf-8",
        )
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 1
        assert "already" in result.output.lower()

    def test_force_overwrites_existing_block(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text(
            "name: my-app\nversion: 1.0.0\ndescription: x\n"
            "marketplace:\n  owner:\n    name: stale-sentinel\n",
            encoding="utf-8",
        )
        result = runner.invoke(marketplace, ["init", "--force"])
        assert result.exit_code == 0
        text = (tmp_path / "apm.yml").read_text(encoding="utf-8")
        assert "stale-sentinel" not in text


# ---------------------------------------------------------------------------
# .gitignore staleness check
# ---------------------------------------------------------------------------


class TestInitGitignoreCheck:
    @pytest.mark.parametrize(
        "pattern",
        ["marketplace.json\n", "**/marketplace.json\n", "/marketplace.json\n"],
    )
    def test_warns_when_gitignore_ignores_marketplace_json(
        self, runner, tmp_path, monkeypatch, pattern,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gitignore").write_text(pattern, encoding="utf-8")
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
    def test_template_is_pure_ascii(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner.invoke(marketplace, ["init"])
        content = (tmp_path / "apm.yml").read_text(encoding="utf-8")
        content.encode("ascii")  # raises UnicodeEncodeError if non-ASCII

    def test_template_has_no_epam_references(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner.invoke(marketplace, ["init"])
        content = (tmp_path / "apm.yml").read_text(encoding="utf-8").lower()
        for forbidden in ("epam", "bookstore", "agent-forge"):
            assert forbidden not in content


# ---------------------------------------------------------------------------
# --name / --owner flags
# ---------------------------------------------------------------------------


class TestInitNameOwnerFlags:
    def test_custom_name_used_for_scaffolded_apm_yml(
        self, runner, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init", "--name", "cool-tools"])
        assert result.exit_code == 0, result.output
        data = yaml.safe_load((tmp_path / "apm.yml").read_text(encoding="utf-8"))
        assert data["name"] == "cool-tools"

    def test_custom_owner(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init", "--owner", "my-org"])
        assert result.exit_code == 0, result.output
        block = _load_marketplace_block(tmp_path / "apm.yml")
        assert block["owner"]["name"] == "my-org"

    def test_default_owner_is_acme_org(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0
        block = _load_marketplace_block(tmp_path / "apm.yml")
        assert block["owner"]["name"] == "acme-org"
