"""Tests for ``apm marketplace upstream {add,list,remove}`` CLI commands."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace

SHA40 = "c" * 40


def _write_yml(tmp_path: Path, content: str | None = None) -> Path:
    if content is None:
        content = textwrap.dedent("""\
            name: acme-marketplace
            description: ACME curated marketplace
            version: 1.0.0
            owner:
              name: ACME Corp
            packages: []
        """)
    p = tmp_path / "marketplace.yml"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def runner():
    return CliRunner()


class TestUpstreamAdd:
    def test_happy_with_ref_and_no_verify(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "upstream",
                "add",
                "abhigyanpatwari/GitNexus",
                "--alias",
                "gitnexus",
                "--ref",
                SHA40,
                "--no-verify",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "gitnexus" in result.output
        data = yaml.safe_load((tmp_path / "marketplace.yml").read_text())
        assert data["upstreams"][0]["alias"] == "gitnexus"
        assert data["upstreams"][0]["ref"] == SHA40

    def test_branch_requires_allow_head(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "upstream",
                "add",
                "a/b",
                "--alias",
                "ok",
                "--branch",
                "main",
                "--no-verify",
            ],
        )
        assert result.exit_code != 0
        assert "allow-head" in result.output.lower()

    def test_ref_xor_branch(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "upstream",
                "add",
                "a/b",
                "--alias",
                "ok",
                "--ref",
                SHA40,
                "--branch",
                "main",
                "--no-verify",
            ],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()

    def test_neither_ref_nor_branch(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            ["upstream", "add", "a/b", "--alias", "ok", "--no-verify"],
        )
        assert result.exit_code != 0
        assert "either --ref" in result.output.lower() or "specify either" in result.output.lower()

    def test_invalid_alias_exits_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(
            marketplace,
            [
                "upstream",
                "add",
                "a/b",
                "--alias",
                "1bad",
                "--ref",
                SHA40,
                "--no-verify",
            ],
        )
        assert result.exit_code == 2
        assert "alias" in result.output.lower()


class TestUpstreamList:
    def test_empty(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(marketplace, ["upstream", "list"])
        assert result.exit_code == 0
        assert "no upstream" in result.output.lower()

    def test_lists_after_add(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        runner.invoke(
            marketplace,
            [
                "upstream",
                "add",
                "abhigyanpatwari/GitNexus",
                "--alias",
                "gitnexus",
                "--ref",
                SHA40,
                "--no-verify",
            ],
        )
        result = runner.invoke(marketplace, ["upstream", "list"])
        assert result.exit_code == 0
        assert "gitnexus" in result.output
        assert "GitNexus" in result.output


class TestUpstreamRemove:
    def test_removes_existing(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        runner.invoke(
            marketplace,
            [
                "upstream",
                "add",
                "a/b",
                "--alias",
                "tobedeleted",
                "--ref",
                SHA40,
                "--no-verify",
            ],
        )
        result = runner.invoke(marketplace, ["upstream", "remove", "tobedeleted"])
        assert result.exit_code == 0
        assert "tobedeleted" in result.output
        data = yaml.safe_load((tmp_path / "marketplace.yml").read_text())
        assert not data.get("upstreams")

    def test_unknown_alias_exits_2(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(tmp_path)
        result = runner.invoke(marketplace, ["upstream", "remove", "ghost"])
        assert result.exit_code == 2
        assert "not found" in result.output.lower()

    def test_blocked_when_referenced(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_yml(
            tmp_path,
            textwrap.dedent("""\
                name: acme-marketplace
                description: ACME
                version: 1.0.0
                owner:
                  name: ACME Corp
                upstreams:
                  - alias: gitnexus
                    repo: a/b
                    ref: cccccccccccccccccccccccccccccccccccccccc
                packages:
                  - name: my-skill
                    upstream: gitnexus
                    plugin: gitnexus
            """),
        )
        result = runner.invoke(marketplace, ["upstream", "remove", "gitnexus"])
        assert result.exit_code == 2
        assert "still referenced" in result.output.lower()
