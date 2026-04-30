"""Integration tests for the unified ``apm pack`` entrypoint.

Covers the matrix of bundle / marketplace / both / neither outputs plus
flag overrides and the hard-error path for the removed
``apm marketplace build`` subcommand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml  # noqa: F401
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace
from apm_cli.commands.pack import pack_cmd

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_LOCKFILE_TEMPLATE = """\
lockfile_version: '1'
generated_at: '2025-01-01T00:00:00+00:00'
dependencies: []
"""


def _write_apm_yml(root: Path, body: str) -> None:
    (root / "apm.yml").write_text(body, encoding="utf-8")


def _write_minimal_lockfile(root: Path) -> None:
    """Write an empty but well-formed apm.lock.yaml so pack_bundle works."""
    (root / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")


def _write_marketplace_block_yml(root: Path, *, package_name: str = "azure") -> None:
    """Write an apm.yml with a marketplace block targeting a local source."""
    plugin_dir = root / ".github" / "plugins" / package_name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    _write_apm_yml(
        root,
        f"""\
name: pack-test
version: 1.0.0
description: pack integration test fixture

marketplace:
  owner:
    name: Tester
    url: https://example.com
  packages:
    - name: {package_name}
      description: Local package fixture for pack integration tests
      source: ./.github/plugins/{package_name}
      homepage: https://example.com
""",
    )


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPackUnified:
    def test_pack_bundle_only(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            "name: x\nversion: 0.1.0\ndescription: y\ndependencies:\n  apm: []\n",
        )
        _write_minimal_lockfile(tmp_path)

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 0, result.output
        # Bundle directory is created under ./build (empty bundle is fine)
        assert (tmp_path / "build").exists()
        # Marketplace.json should NOT be created
        assert not (tmp_path / ".claude-plugin" / "marketplace.json").exists()

    def test_pack_marketplace_only(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 0, result.output
        out = tmp_path / ".claude-plugin" / "marketplace.json"
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["name"] == "pack-test"
        assert data["plugins"][0]["name"] == "azure"
        # No bundle directory should appear
        assert not (tmp_path / "build").exists()

    def test_pack_both(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Add both blocks
        plugin_dir = tmp_path / ".github" / "plugins" / "azure"
        plugin_dir.mkdir(parents=True)
        _write_apm_yml(
            tmp_path,
            """\
name: pack-test
version: 1.0.0
description: y
dependencies:
  apm: []

marketplace:
  owner:
    name: Tester
    url: https://example.com
  packages:
    - name: azure
      description: x
      source: ./.github/plugins/azure
""",
        )
        _write_minimal_lockfile(tmp_path)

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 0, result.output
        assert (tmp_path / "build").exists()
        assert (tmp_path / ".claude-plugin" / "marketplace.json").exists()

    def test_pack_neither_errors(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path, "name: x\nversion: 0.1.0\ndescription: y\n")

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 1
        assert "Nothing to pack" in result.output

    def test_pack_marketplace_output_override(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)

        out_path = tmp_path / "out" / "m.json"
        result = runner.invoke(pack_cmd, ["--marketplace-output", str(out_path)])

        assert result.exit_code == 0, result.output
        assert out_path.exists()
        # Default location should NOT be written when overridden
        assert not (tmp_path / ".claude-plugin" / "marketplace.json").exists()

    def test_pack_legacy_marketplace_yml(self, runner, tmp_path, monkeypatch):
        """Legacy standalone marketplace.yml still produces marketplace.json."""
        monkeypatch.chdir(tmp_path)
        plugin_dir = tmp_path / ".github" / "plugins" / "azure"
        plugin_dir.mkdir(parents=True)
        # apm.yml has neither dependencies nor marketplace blocks
        _write_apm_yml(tmp_path, "name: x\nversion: 0.1.0\ndescription: y\n")
        (tmp_path / "marketplace.yml").write_text(
            """\
name: legacy
version: 0.1.0
description: y
owner:
  name: Tester
  url: https://example.com
packages:
  - name: azure
    description: x
    source: ./.github/plugins/azure
""",
            encoding="utf-8",
        )

        result = runner.invoke(pack_cmd, [])

        assert result.exit_code == 0, result.output
        # Legacy default path is ./marketplace.json (kept by yml_schema)
        assert (tmp_path / "marketplace.json").exists()
        # Deprecation warning should fire
        assert "marketplace.yml" in result.output.lower()

    def test_pack_dry_run_marketplace(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_marketplace_block_yml(tmp_path)

        result = runner.invoke(pack_cmd, ["--dry-run"])

        assert result.exit_code == 0, result.output
        assert not (tmp_path / ".claude-plugin" / "marketplace.json").exists()

    def test_pack_plugin_format_with_marketplace(self, runner, tmp_path, monkeypatch):
        """--format plugin still triggers marketplace producer."""
        monkeypatch.chdir(tmp_path)
        plugin_dir = tmp_path / ".github" / "plugins" / "azure"
        plugin_dir.mkdir(parents=True)
        _write_apm_yml(
            tmp_path,
            """\
name: pack-test
version: 1.0.0
description: y
dependencies:
  apm: []

marketplace:
  owner:
    name: Tester
    url: https://example.com
  packages:
    - name: azure
      description: x
      source: ./.github/plugins/azure
""",
        )
        _write_minimal_lockfile(tmp_path)

        result = runner.invoke(pack_cmd, ["--format", "plugin"])

        assert result.exit_code == 0, result.output
        # Marketplace.json must be written regardless of bundle format
        assert (tmp_path / ".claude-plugin" / "marketplace.json").exists()


# ---------------------------------------------------------------------------
# Removed `apm marketplace build` subcommand
# ---------------------------------------------------------------------------


class TestMarketplaceBuildSubcommandRemoved:
    def test_marketplace_build_subcommand_errors(self, runner):
        result = runner.invoke(marketplace, ["build"])
        # Click maps UsageError to exit code 2.
        assert result.exit_code == 2
        assert "apm pack" in result.output
        assert "was removed" in result.output
