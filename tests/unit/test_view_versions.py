"""Unit tests for marketplace plugin display in ``apm view``."""

from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from apm_cli.commands.view import _display_marketplace_plugin
from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _plugin(
    name="skill-auth",
    version="2.1.0",
    description="Authentication skill",
    ref="v2.1.0",
    tags=("security", "auth"),
):
    """Build a MarketplacePlugin with a github source."""
    return MarketplacePlugin(
        name=name,
        source={"type": "github", "repo": "acme-org/skill-auth", "ref": ref},
        version=version,
        description=description,
        tags=tags,
    )


def _manifest(*plugins, name="acme-tools"):
    return MarketplaceManifest(name=name, plugins=tuple(plugins))


def _source(name="acme-tools"):
    return MarketplaceSource(name=name, owner="acme-org", repo="marketplace")


# ---------------------------------------------------------------------------
# _display_marketplace_plugin
# ---------------------------------------------------------------------------


class TestDisplayMarketplacePlugin:
    """Plugin metadata display."""

    @patch("apm_cli.marketplace.client.fetch_or_cache")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_displays_plugin_info(self, mock_get_mkt, mock_fetch, capsys):
        """Renders plugin name, version, description, source."""
        mock_get_mkt.return_value = _source()
        mock_fetch.return_value = _manifest(_plugin())

        logger = MagicMock()
        _display_marketplace_plugin("skill-auth", "acme-tools", logger)

        # The output goes through Rich or click.echo - check no errors
        logger.error.assert_not_called()

    @patch("apm_cli.marketplace.client.fetch_or_cache")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_plugin_not_found_exits(self, mock_get_mkt, mock_fetch):
        """Unknown plugin triggers error and sys.exit."""
        mock_get_mkt.return_value = _source()
        mock_fetch.return_value = _manifest(_plugin(name="other"))

        logger = MagicMock()
        with pytest.raises(SystemExit):
            _display_marketplace_plugin("nonexistent", "acme-tools", logger)
        logger.error.assert_called_once()

    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_marketplace_not_found_exits(self, mock_get_mkt):
        """Unknown marketplace triggers error and sys.exit."""
        from apm_cli.marketplace.errors import MarketplaceNotFoundError
        mock_get_mkt.side_effect = MarketplaceNotFoundError("nope")

        logger = MagicMock()
        with pytest.raises(SystemExit):
            _display_marketplace_plugin("any", "nope", logger)

    @patch("apm_cli.marketplace.client.fetch_or_cache")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_string_source_handled(self, mock_get_mkt, mock_fetch):
        """Plugin with string source (shorthand) does not crash."""
        plugin = MarketplacePlugin(
            name="simple",
            source="acme-org/simple",
            version="1.0.0",
        )
        mock_get_mkt.return_value = _source()
        mock_fetch.return_value = _manifest(plugin)

        logger = MagicMock()
        _display_marketplace_plugin("simple", "acme-tools", logger)
        logger.error.assert_not_called()

    @patch("apm_cli.marketplace.client.fetch_or_cache")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_no_version_no_description(self, mock_get_mkt, mock_fetch):
        """Plugin without optional fields renders without error."""
        plugin = MarketplacePlugin(
            name="bare",
            source={"type": "github", "repo": "o/r", "ref": "main"},
        )
        mock_get_mkt.return_value = _source()
        mock_fetch.return_value = _manifest(plugin)

        logger = MagicMock()
        _display_marketplace_plugin("bare", "acme-tools", logger)
        logger.error.assert_not_called()
