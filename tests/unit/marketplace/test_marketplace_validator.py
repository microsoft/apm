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
    ValidationResult,
    validate_marketplace,
    validate_no_duplicate_names,
    validate_plugin_schema,
    validate_raw_marketplace_structure,
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
# Unit tests -- validate_raw_marketplace_structure
# ===================================================================


class TestValidateRawMarketplaceStructure:
    """validate_raw_marketplace_structure catches structural violations."""

    def test_valid_dict_with_list_plugins_passes(self):
        data = {"name": "acme", "plugins": [{"name": "a", "source": "owner/a"}]}
        result = validate_raw_marketplace_structure(data)
        assert result.check_name == "Structure"
        assert result.passed is True
        assert result.errors == []

    def test_valid_dict_missing_plugins_key_passes(self):
        """Absent plugins key is fine -- defaults to empty list on parse."""
        result = validate_raw_marketplace_structure({"name": "acme"})
        assert result.passed is True

    def test_plugins_string_fails(self):
        data = {"plugins": "not-an-array"}
        result = validate_raw_marketplace_structure(data)
        assert result.passed is False
        assert any("plugins" in e and "array" in e for e in result.errors)

    def test_plugins_dict_fails(self):
        data = {"plugins": {"name": "a"}}
        result = validate_raw_marketplace_structure(data)
        assert result.passed is False
        assert any("plugins" in e for e in result.errors)

    def test_plugins_integer_fails(self):
        data = {"plugins": 42}
        result = validate_raw_marketplace_structure(data)
        assert result.passed is False
        assert any("plugins" in e for e in result.errors)

    def test_plugins_null_fails(self):
        data = {"plugins": None}
        result = validate_raw_marketplace_structure(data)
        assert result.passed is False
        assert any("plugins" in e for e in result.errors)

    def test_plugins_empty_list_passes(self):
        result = validate_raw_marketplace_structure({"plugins": []})
        assert result.passed is True

    def test_non_dict_root_fails(self):
        result = validate_raw_marketplace_structure(["a", "b"])
        assert result.passed is False
        assert any("root" in e or "object" in e for e in result.errors)


# ===================================================================
# CLI command tests -- apm marketplace validate
# ===================================================================


def _raw_dict(name="test-marketplace", plugins=None):
    """Build a raw marketplace.json dict for test mocking."""
    return {"name": name, "plugins": plugins if plugins is not None else []}


def _raw_plugin(name="test-plugin", source="owner/repo"):
    return {"name": name, "source": source}


class TestValidateCommand:
    """CLI command output and behavior."""

    @patch("apm_cli.marketplace.client.fetch_marketplace_raw")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_output_format(self, mock_get, mock_fetch_raw, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch_raw.return_value = _raw_dict(
            plugins=[_raw_plugin("a", "owner/a"), _raw_plugin("b", "owner/b")]
        )
        result = runner.invoke(marketplace, ["validate", "acme"])
        assert result.exit_code == 0
        assert "Validating marketplace" in result.output
        assert "Validation Results:" in result.output
        assert "Summary:" in result.output
        assert "passed" in result.output

    @patch("apm_cli.marketplace.client.fetch_marketplace_raw")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_verbose_shows_per_plugin_details(self, mock_get, mock_fetch_raw, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch_raw.return_value = _raw_dict(plugins=[_raw_plugin("alpha", "owner/alpha")])
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

    @patch("apm_cli.marketplace.client.fetch_marketplace_raw")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_check_refs_shows_warning(self, mock_get, mock_fetch_raw, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch_raw.return_value = _raw_dict(plugins=[_raw_plugin("a", "owner/a")])
        result = runner.invoke(marketplace, ["validate", "acme", "--check-refs"])
        assert result.exit_code == 0
        assert "not yet implemented" in result.output.lower()

    @patch("apm_cli.marketplace.client.fetch_marketplace_raw")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_plugin_count_in_output(self, mock_get, mock_fetch_raw, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch_raw.return_value = _raw_dict(
            plugins=[_raw_plugin("a", "o/a"), _raw_plugin("b", "o/b"), _raw_plugin("c", "o/c")]
        )
        result = runner.invoke(marketplace, ["validate", "acme"])
        assert result.exit_code == 0
        assert "3 plugins" in result.output

    @patch("apm_cli.marketplace.validator.validate_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace_raw")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_warnings_only_result_shown(self, mock_get, mock_fetch_raw, mock_validate, runner):
        """ValidationResult with warnings but no errors renders warning lines."""
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch_raw.return_value = _raw_dict(plugins=[_raw_plugin("a", "owner/a")])
        mock_validate.return_value = [
            ValidationResult(
                check_name="Schema",
                passed=True,
                warnings=["minor warning here"],
                errors=[],
            ),
        ]
        result = runner.invoke(marketplace, ["validate", "acme"])
        assert result.exit_code == 0
        assert "minor warning here" in result.output

    @patch("apm_cli.marketplace.validator.validate_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace_raw")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_errors_result_exits_1(self, mock_get, mock_fetch_raw, mock_validate, runner):
        """ValidationResult with errors causes sys.exit(1)."""
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch_raw.return_value = _raw_dict(plugins=[_raw_plugin("a", "owner/a")])
        mock_validate.return_value = [
            ValidationResult(
                check_name="Schema",
                passed=False,
                warnings=[],
                errors=["critical error"],
            ),
        ]
        result = runner.invoke(marketplace, ["validate", "acme"])
        assert result.exit_code == 1
        assert "critical error" in result.output

    @patch("apm_cli.marketplace.validator.validate_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace_raw")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_errors_and_warnings_both_shown(self, mock_get, mock_fetch_raw, mock_validate, runner):
        """ValidationResult with both errors and warnings renders both."""
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch_raw.return_value = _raw_dict(plugins=[_raw_plugin("a", "owner/a")])
        mock_validate.return_value = [
            ValidationResult(
                check_name="Schema",
                passed=False,
                warnings=["a warning"],
                errors=["an error"],
            ),
        ]
        result = runner.invoke(marketplace, ["validate", "acme"])
        assert result.exit_code == 1
        assert "an error" in result.output
        assert "a warning" in result.output

    @patch("apm_cli.marketplace.client.fetch_marketplace_raw")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_malformed_plugins_exits_1(self, mock_get, mock_fetch_raw, runner):
        """A non-list 'plugins' field must be caught and reported as an error."""
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch_raw.return_value = {"name": "acme", "plugins": "not-an-array"}
        result = runner.invoke(marketplace, ["validate", "acme"])
        assert result.exit_code == 1
        assert "plugins" in result.output.lower()
        assert "array" in result.output.lower() or "Structure" in result.output

    @patch("apm_cli.marketplace.client.fetch_marketplace_raw")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_malformed_plugins_null_exits_1(self, mock_get, mock_fetch_raw, runner):
        """A null 'plugins' field must be caught and reported as an error."""
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch_raw.return_value = {"name": "acme", "plugins": None}
        result = runner.invoke(marketplace, ["validate", "acme"])
        assert result.exit_code == 1
        assert "plugins" in result.output.lower()

    @patch("apm_cli.marketplace.client.fetch_marketplace_raw")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_non_dict_root_exits_1(self, mock_get, mock_fetch_raw, runner):
        """A non-dict root (list, string) must be caught without raising AttributeError."""
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch_raw.return_value = [{"name": "a", "source": "owner/a"}]
        result = runner.invoke(marketplace, ["validate", "acme"])
        assert result.exit_code == 1
        assert "Structure" in result.output or "root" in result.output.lower()
        assert (
            "object" in result.output.lower()
            or "dict" in result.output.lower()
            or "list" in result.output.lower()
        )
