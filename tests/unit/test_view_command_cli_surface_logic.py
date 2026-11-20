"""tests for apm_cli.commands.view (pure logic cases)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger():
    m = MagicMock()
    m.verbose = False
    return m


def _make_remote_ref(name="v1.0.0", ref_type_val="tag", sha="abc12345"):
    from apm_cli.models.dependency.types import GitReferenceType, RemoteRef

    ref_type = GitReferenceType.TAG if ref_type_val == "tag" else GitReferenceType.BRANCH
    return RemoteRef(name=name, ref_type=ref_type, commit_sha=sha)


# ---------------------------------------------------------------------------
# resolve_package_path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# display_package_info
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _display_marketplace_plugin
# ---------------------------------------------------------------------------


class TestDisplayMarketplacePlugin:
    def _build_plugin(self, *, source=None, version="1.0.0", tags=None, description="Desc"):
        plugin = MagicMock()
        plugin.name = "my-plugin"
        plugin.version = version
        plugin.description = description
        plugin.source = source or {"type": "github", "repo": "org/repo", "ref": "v1.0.0"}
        plugin.tags = tags or ["ai", "code"]
        return plugin

    def test_get_marketplace_by_name_error_exits(self):
        from apm_cli.commands.view import _display_marketplace_plugin

        logger = _make_logger()
        with (
            patch(
                "apm_cli.marketplace.registry.get_marketplace_by_name",
                side_effect=Exception("not found"),
            ),
            pytest.raises(SystemExit),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)

    def test_fetch_or_cache_error_exits(self):
        from apm_cli.commands.view import _display_marketplace_plugin
        from apm_cli.marketplace.errors import MarketplaceFetchError

        logger = _make_logger()
        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch(
                "apm_cli.marketplace.client.fetch_or_cache",
                side_effect=MarketplaceFetchError("fetch failed"),
            ),
            pytest.raises(SystemExit),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)

    def test_plugin_not_found_exits(self):
        from apm_cli.commands.view import _display_marketplace_plugin

        logger = _make_logger()
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = None

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
            pytest.raises(SystemExit),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)

    def test_dict_source_with_ref_rendered(self, capsys):
        from apm_cli.commands.view import _display_marketplace_plugin

        logger = _make_logger()
        plugin = self._build_plugin(source={"type": "github", "repo": "org/repo", "ref": "v2.0"})
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = plugin

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                return_value=("org/repo#v2.0", MagicMock()),
            ),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)

    def test_string_source_rendered(self, capsys):
        from apm_cli.commands.view import _display_marketplace_plugin

        logger = _make_logger()
        plugin = self._build_plugin(source="github/org/repo")
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = plugin

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
            patch(
                "apm_cli.marketplace.resolver.resolve_marketplace_plugin",
                side_effect=Exception("resolve failed"),
            ),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)

    def test_no_version_no_description_no_tags(self, capsys):
        from apm_cli.commands.view import _display_marketplace_plugin

        logger = _make_logger()
        plugin = self._build_plugin(version=None, tags=None, description=None)
        plugin.version = None
        plugin.description = None
        plugin.tags = None
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = plugin

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)

    def test_import_error_fallback_text_output(self):
        """Covers Rich ImportError fallback in _display_marketplace_plugin."""
        from apm_cli.commands.view import _display_marketplace_plugin

        logger = _make_logger()
        plugin = self._build_plugin(
            source={"type": "github", "repo": "org/repo"},
        )
        plugin.tags = ["tag1"]
        mock_manifest = MagicMock()
        mock_manifest.find_plugin.return_value = plugin

        with (
            patch("apm_cli.marketplace.registry.get_marketplace_by_name", return_value=MagicMock()),
            patch("apm_cli.marketplace.client.fetch_or_cache", return_value=mock_manifest),
            patch.dict(sys.modules, {"rich.console": None, "rich.panel": None}),
        ):
            _display_marketplace_plugin("my-plugin", "mymarket", logger)


# ---------------------------------------------------------------------------
# display_versions
# ---------------------------------------------------------------------------


class TestDisplayVersions:
    def test_marketplace_ref_dispatches_to_display_marketplace(self):
        from apm_cli.commands.view import display_versions

        logger = _make_logger()
        with (
            patch(
                "apm_cli.marketplace.resolver.parse_marketplace_ref",
                return_value=("plugin", "market", None),
            ),
            patch("apm_cli.commands.view._display_marketplace_plugin") as mock_disp,
        ):
            display_versions("plugin@market", logger)
        mock_disp.assert_called_once()

    def test_invalid_package_ref_exits(self):
        from apm_cli.commands.view import display_versions

        logger = _make_logger()
        with (
            patch("apm_cli.marketplace.resolver.parse_marketplace_ref", return_value=None),
            patch(
                "apm_cli.models.dependency.reference.DependencyReference.parse",
                side_effect=ValueError("bad ref"),
            ),
            pytest.raises(SystemExit),
        ):
            display_versions("bad::ref", logger)

    def test_list_remote_refs_runtime_error_exits(self):
        from apm_cli.commands.view import display_versions

        logger = _make_logger()
        mock_downloader = MagicMock()
        mock_downloader.list_remote_refs.side_effect = RuntimeError("network error")

        with (
            patch("apm_cli.marketplace.resolver.parse_marketplace_ref", return_value=None),
            patch(
                "apm_cli.commands.view.GitHubPackageDownloader",
                return_value=mock_downloader,
            ),
            pytest.raises(SystemExit),
        ):
            display_versions("org/repo", logger)

    def test_no_refs_logs_warning(self):
        from apm_cli.commands.view import display_versions

        logger = _make_logger()
        mock_downloader = MagicMock()
        mock_downloader.list_remote_refs.return_value = []

        with (
            patch("apm_cli.marketplace.resolver.parse_marketplace_ref", return_value=None),
            patch(
                "apm_cli.commands.view.GitHubPackageDownloader",
                return_value=mock_downloader,
            ),
        ):
            display_versions("org/repo", logger)
        logger.warning.assert_called()

    def test_rich_table_rendered_with_refs(self):
        from apm_cli.commands.view import display_versions

        logger = _make_logger()
        ref = _make_remote_ref("v1.0.0", "tag", "abc12345678901234")
        mock_downloader = MagicMock()
        mock_downloader.list_remote_refs.return_value = [ref]

        with (
            patch("apm_cli.marketplace.resolver.parse_marketplace_ref", return_value=None),
            patch(
                "apm_cli.commands.view.GitHubPackageDownloader",
                return_value=mock_downloader,
            ),
        ):
            display_versions("org/repo", logger)

    def test_import_error_plain_text_fallback(self, capsys):
        from apm_cli.commands.view import display_versions

        logger = _make_logger()
        ref = _make_remote_ref("v2.0.0", "tag", "deadbeef12345678")
        mock_downloader = MagicMock()
        mock_downloader.list_remote_refs.return_value = [ref]

        with (
            patch("apm_cli.marketplace.resolver.parse_marketplace_ref", return_value=None),
            patch(
                "apm_cli.commands.view.GitHubPackageDownloader",
                return_value=mock_downloader,
            ),
            patch.dict(sys.modules, {"rich.console": None, "rich.table": None}),
        ):
            display_versions("org/repo", logger)


# ---------------------------------------------------------------------------
# view CLI command
# ---------------------------------------------------------------------------
