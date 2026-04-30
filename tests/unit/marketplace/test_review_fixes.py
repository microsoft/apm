"""Regression tests for PR #1038 review-comment fixes.

Covers the edge cases flagged by copilot-pull-request-reviewer:
- malformed apm.yml surfaces a clear error instead of "no marketplace config"
- empty / non-mapping apm.yml does not crash migrate or init
- ``_is_apm_yml_with_marketplace`` rejects non-mapping marketplace values
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace
from apm_cli.marketplace.errors import MarketplaceYmlError
from apm_cli.marketplace.migration import (
    _has_marketplace_block,
    detect_config_source,
    migrate_marketplace_yml,
)
from apm_cli.marketplace.yml_editor import _is_apm_yml_with_marketplace

# ---------------------------------------------------------------------------
# r3: malformed apm.yml surfaces a clear error
# ---------------------------------------------------------------------------


class TestMalformedApmYmlSurfaced:
    def test_yaml_parse_error_raises_marketplace_error(self, tmp_path: Path):
        bad = tmp_path / "apm.yml"
        bad.write_text("name: app\nversion: : :\n", encoding="utf-8")
        with pytest.raises(MarketplaceYmlError, match="Invalid YAML"):
            _has_marketplace_block(bad)

    def test_unreadable_apm_yml_raises_marketplace_error(self, tmp_path: Path, monkeypatch):
        bad = tmp_path / "apm.yml"
        bad.write_text("name: app\n", encoding="utf-8")

        def boom(*a, **kw):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", boom)
        with pytest.raises(MarketplaceYmlError, match="Could not read"):
            _has_marketplace_block(bad)

    def test_detect_config_source_propagates_parse_error(self, tmp_path: Path):
        bad = tmp_path / "apm.yml"
        bad.write_text("name: app\nversion: : :\n", encoding="utf-8")
        with pytest.raises(MarketplaceYmlError):
            detect_config_source(tmp_path)


# ---------------------------------------------------------------------------
# r4: migrate handles empty / non-mapping apm.yml
# ---------------------------------------------------------------------------


def _legacy_yml() -> str:
    return "name: mp\nversion: 0.1.0\ndescription: legacy mp\nowner:\n  name: acme\npackages: []\n"


class TestMigrateNonMappingApmYml:
    def test_migrate_rejects_list_top_level(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text("- one\n- two\n", encoding="utf-8")
        (tmp_path / "marketplace.yml").write_text(_legacy_yml(), encoding="utf-8")
        with pytest.raises(MarketplaceYmlError, match="must be a YAML mapping"):
            migrate_marketplace_yml(tmp_path)

    def test_migrate_rejects_scalar_top_level(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text("just-a-string\n", encoding="utf-8")
        (tmp_path / "marketplace.yml").write_text(_legacy_yml(), encoding="utf-8")
        with pytest.raises(MarketplaceYmlError, match="must be a YAML mapping"):
            migrate_marketplace_yml(tmp_path)

    def test_migrate_handles_empty_apm_yml(self, tmp_path: Path):
        (tmp_path / "apm.yml").write_text("", encoding="utf-8")
        (tmp_path / "marketplace.yml").write_text(_legacy_yml(), encoding="utf-8")
        # Should succeed: empty apm.yml round-trips to an empty mapping.
        diff = migrate_marketplace_yml(tmp_path)
        assert "marketplace" in (tmp_path / "apm.yml").read_text(encoding="utf-8")
        assert diff  # diff is a non-empty unified-diff string


# ---------------------------------------------------------------------------
# r5: _is_apm_yml_with_marketplace rejects non-mapping marketplace values
# ---------------------------------------------------------------------------


class TestIsApmYmlWithMarketplaceTightened:
    def test_marketplace_value_must_be_mapping(self):
        # Scalar marketplace value is not a valid block; helper must say no.
        data = {"name": "app", "marketplace": "not-a-block"}
        assert _is_apm_yml_with_marketplace(data) is False

    def test_marketplace_list_value_rejected(self):
        data = {"name": "app", "marketplace": [1, 2, 3]}
        assert _is_apm_yml_with_marketplace(data) is False

    def test_marketplace_mapping_accepted(self):
        data = {"name": "app", "marketplace": {"owner": {"name": "acme"}}}
        assert _is_apm_yml_with_marketplace(data) is True

    def test_missing_or_null_block_rejected(self):
        assert _is_apm_yml_with_marketplace({"name": "app"}) is False
        assert _is_apm_yml_with_marketplace({"marketplace": None}) is False


# ---------------------------------------------------------------------------
# r6: marketplace init handles empty / non-mapping apm.yml
# ---------------------------------------------------------------------------


class TestMarketplaceInitNonMappingApmYml:
    def test_init_rejects_list_top_level(self, tmp_path: Path, monkeypatch):
        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text("- one\n", encoding="utf-8")
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 1
        assert "must be a YAML mapping" in result.output

    def test_init_handles_empty_apm_yml(self, tmp_path: Path, monkeypatch):
        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text("", encoding="utf-8")
        result = runner.invoke(marketplace, ["init"])
        assert result.exit_code == 0, result.output
        text = (tmp_path / "apm.yml").read_text(encoding="utf-8")
        assert "marketplace:" in text


# ---------------------------------------------------------------------------
# Followup: migrate -- malformed apm.yml raises typed MarketplaceYmlError
# (instead of leaking a raw ruamel.yaml.YAMLError)
# ---------------------------------------------------------------------------


class TestMigrateMalformedApmYmlTyped:
    def test_migrate_with_malformed_apm_yml_raises_typed_error(self, tmp_path: Path):
        # apm.yml passes the up-front existence check but is unparseable.
        (tmp_path / "apm.yml").write_text("name: app\nversion: : :\n", encoding="utf-8")
        (tmp_path / "marketplace.yml").write_text(_legacy_yml(), encoding="utf-8")
        with pytest.raises(MarketplaceYmlError, match="apm.yml is malformed"):  # noqa: RUF043
            migrate_marketplace_yml(tmp_path)


# ---------------------------------------------------------------------------
# Followup: apm init --marketplace warns when experimental flag is disabled
# ---------------------------------------------------------------------------

# Removed: the ``marketplace_authoring`` experimental flag was deleted when
# marketplace authoring went GA. ``apm init --marketplace`` now appends the
# block unconditionally, so the disabled-flag warning case no longer exists.
