"""Tests for marketplace manifest validator and validate CLI command."""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.marketplace.validator import (
    ValidationResult,  # noqa: F401
    validate_marketplace,
    validate_no_duplicate_names,
    validate_plugin_schema,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Isolate filesystem writes (mirrors test_marketplace_commands.py)."""
    config_dir = str(tmp_path / ".apm")
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _plugin(name="test-plugin", source="owner/repo"):
    """Convenience builder for a MarketplacePlugin."""
    return MarketplacePlugin(name=name, source=source)


def _manifest(*plugins, name="test-marketplace"):
    """Convenience builder for a MarketplaceManifest."""
    return MarketplaceManifest(name=name, plugins=tuple(plugins))


# ===================================================================
# Unit tests -- validate_plugin_schema
# ===================================================================


class TestValidatePluginSchema:
    """validate_plugin_schema checks name + source are present."""

    def test_valid_plugins_pass(self):
        plugins = [_plugin("a", "owner/a"), _plugin("b", "owner/b")]
        result = validate_plugin_schema(plugins)
        assert result.passed is True
        assert result.errors == []

    def test_plugin_missing_name(self):
        plugins = [_plugin(name="", source="owner/repo")]
        result = validate_plugin_schema(plugins)
        assert result.passed is False
        assert any("empty name" in e for e in result.errors)

    def test_plugin_missing_source(self):
        plugins = [MarketplacePlugin(name="orphan", source=None)]
        result = validate_plugin_schema(plugins)
        assert result.passed is False
        assert any("source" in e.lower() for e in result.errors)

    def test_empty_list_passes(self):
        result = validate_plugin_schema([])
        assert result.passed is True


# ===================================================================
# Unit tests -- validate_no_duplicate_names
# ===================================================================


class TestValidateNoDuplicateNames:
    """validate_no_duplicate_names is case-insensitive."""

    def test_unique_names_pass(self):
        plugins = [_plugin(name="alpha"), _plugin(name="beta")]
        result = validate_no_duplicate_names(plugins)
        assert result.passed is True
        assert result.errors == []

    def test_duplicate_names_case_insensitive(self):
        plugins = [_plugin(name="MyPlugin"), _plugin(name="myplugin")]
        result = validate_no_duplicate_names(plugins)
        assert result.passed is False
        assert len(result.errors) == 1
        assert "myplugin" in result.errors[0].lower()

    def test_empty_list_passes(self):
        result = validate_no_duplicate_names([])
        assert result.passed is True


# ===================================================================
# Unit tests -- validate_marketplace (integration of all checks)
# ===================================================================


class TestValidateMarketplace:
    """validate_marketplace returns all check results."""

    def test_valid_marketplace_returns_all_passed(self):
        manifest = _manifest(
            _plugin("a", "owner/a"),
            _plugin("b", "owner/b"),
        )
        results = validate_marketplace(manifest)
        assert len(results) == 2
        assert all(r.passed for r in results)

    def test_empty_marketplace_passes_all(self):
        manifest = _manifest()
        results = validate_marketplace(manifest)
        assert len(results) == 2
        assert all(r.passed for r in results)


# ===================================================================
# CLI command tests -- apm marketplace validate
# ===================================================================


class TestValidateCommand:
    """CLI command output and behavior."""

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_output_format(self, mock_get, mock_fetch, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch.return_value = _manifest(
            _plugin("a", "owner/a"),
            _plugin("b", "owner/b"),
        )
        result = runner.invoke(marketplace, ["validate", "acme"])
        assert result.exit_code == 0
        assert "Validating marketplace" in result.output
        assert "Validation Results:" in result.output
        assert "Summary:" in result.output
        assert "passed" in result.output

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_verbose_shows_per_plugin_details(self, mock_get, mock_fetch, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch.return_value = _manifest(
            _plugin("alpha", "owner/alpha"),
        )
        result = runner.invoke(marketplace, ["validate", "acme", "--verbose"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "source type" in result.output

    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_unregistered_marketplace_errors(self, mock_get, runner):
        from apm_cli.commands.marketplace import marketplace
        from apm_cli.marketplace.errors import MarketplaceNotFoundError

        mock_get.side_effect = MarketplaceNotFoundError("nope")
        result = runner.invoke(marketplace, ["validate", "nope"])
        assert result.exit_code != 0

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_check_refs_shows_warning(self, mock_get, mock_fetch, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch.return_value = _manifest(
            _plugin("a", "owner/a"),
        )
        result = runner.invoke(marketplace, ["validate", "acme", "--check-refs"])
        assert result.exit_code == 0
        assert "not yet implemented" in result.output.lower()

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_plugin_count_in_output(self, mock_get, mock_fetch, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch.return_value = _manifest(
            _plugin("a", "o/a"),
            _plugin("b", "o/b"),
            _plugin("c", "o/c"),
        )
        result = runner.invoke(marketplace, ["validate", "acme"])
        assert result.exit_code == 0
        assert "3 plugins" in result.output
