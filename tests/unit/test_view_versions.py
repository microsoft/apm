"""Unit tests for marketplace plugin display in ``apm view``."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner  # noqa: F401

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


# ---------------------------------------------------------------------------
# Registry-only mode (PROXY_REGISTRY_ONLY)
# ---------------------------------------------------------------------------


class TestDisplayVersionsRegistryOnly:
    """Version listing must not reach the source host in registry-only mode."""

    def test_routes_to_proxy_registry(self):
        """PROXY_REGISTRY_ONLY routes ``view versions`` to the proxy registry."""
        from types import SimpleNamespace

        from apm_cli.commands import view

        proxy = SimpleNamespace(url="https://proxy.example.com/artifactory/apm")
        logger = MagicMock()

        with (
            patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=True),
            patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=proxy),
            patch.object(view, "_display_registry_versions") as display,
            patch("apm_cli.commands.view.GitHubPackageDownloader.list_remote_refs") as ls_remote,
        ):
            view.display_versions("acme-org/acme-pkg", logger)

        display.assert_called_once()
        assert display.call_args.kwargs["registry_url_override"] == proxy.url
        ls_remote.assert_not_called()

    def test_missing_proxy_url_exits(self):
        """PROXY_REGISTRY_ONLY without a proxy URL fails with a clear error."""
        from apm_cli.commands import view

        logger = MagicMock()

        with (
            patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=True),
            patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=None),
            patch("apm_cli.commands.view.GitHubPackageDownloader.list_remote_refs") as ls_remote,
        ):
            with pytest.raises(SystemExit) as exc:
                view.display_versions("acme-org/acme-pkg", logger)

        assert exc.value.code == 1
        assert "PROXY_REGISTRY_URL" in logger.error.call_args.args[0]
        ls_remote.assert_not_called()
