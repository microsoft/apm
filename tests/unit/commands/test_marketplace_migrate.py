"""Tests for ``apm marketplace migrate``."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace
from apm_cli.core import experimental


def _write(p: Path, content: str) -> None:
    p.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


_LEGACY = """\
name: my-marketplace
description: A marketplace.
version: 1.0.0
owner:
  name: ACME
  url: https://github.com/acme
build:
  tagPattern: "v{version}"
packages:
  - name: tool-a
    source: acme/tool-a
    ref: main
"""


_APM = """\
name: my-project
description: A project.
version: 1.0.0
"""


@pytest.fixture(autouse=True)
def _enable_authoring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authoring commands are gated behind an experimental flag."""
    monkeypatch.setattr(experimental, "is_enabled", lambda _: True)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


class TestMigrateHappyPath:
    def test_migrate_writes_block_and_removes_legacy(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / "apm.yml", _APM)
        _write(tmp_path / "marketplace.yml", _LEGACY)

        result = runner.invoke(marketplace, ["migrate"])
        assert result.exit_code == 0, result.output
        assert "Migrated" in result.output
        assert not (tmp_path / "marketplace.yml").exists()

        new_apm = (tmp_path / "apm.yml").read_text(encoding="utf-8")
        assert "marketplace:" in new_apm
        assert "owner:" in new_apm
        assert "tool-a" in new_apm

    def test_migrate_dry_run_keeps_legacy(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / "apm.yml", _APM)
        _write(tmp_path / "marketplace.yml", _LEGACY)

        result = runner.invoke(marketplace, ["migrate", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert (tmp_path / "marketplace.yml").exists()
        # apm.yml unchanged
        assert (tmp_path / "apm.yml").read_text(encoding="utf-8") == _APM


class TestMigrateGuards:
    def test_missing_legacy_yml(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / "apm.yml", _APM)
        result = runner.invoke(marketplace, ["migrate"])
        assert result.exit_code == 1
        assert "marketplace.yml not found" in result.output

    def test_missing_apm_yml(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / "marketplace.yml", _LEGACY)
        result = runner.invoke(marketplace, ["migrate"])
        assert result.exit_code == 1
        assert "apm.yml not found" in result.output

    def test_existing_block_without_force(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / "apm.yml", _APM + "marketplace:\n  owner:\n    name: X\n")
        _write(tmp_path / "marketplace.yml", _LEGACY)
        result = runner.invoke(marketplace, ["migrate"])
        assert result.exit_code == 1
        assert "--force" in result.output

    def test_existing_block_with_force_overwrites(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / "apm.yml", _APM + "marketplace:\n  owner:\n    name: X\n")
        _write(tmp_path / "marketplace.yml", _LEGACY)
        result = runner.invoke(marketplace, ["migrate", "--force"])
        assert result.exit_code == 0, result.output
        new_apm = (tmp_path / "apm.yml").read_text(encoding="utf-8")
        assert "ACME" in new_apm  # legacy owner.name was 'ACME'
