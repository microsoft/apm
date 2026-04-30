"""Tests for marketplace CLI commands using CliRunner."""

import json  # noqa: F401
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest
from click.testing import CliRunner

from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Isolate filesystem writes."""
    config_dir = str(tmp_path / ".apm")
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)


class TestMarketplaceAdd:
    """marketplace add OWNER/REPO."""

    def test_invalid_format_no_slash(self, runner):
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(marketplace, ["add", "just-a-name"])
        assert result.exit_code != 0
        assert "OWNER/REPO" in result.output

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_uses_manifest_name_when_available(self, mock_detect, mock_fetch, mock_add, runner):
        """Manifest's `name` field becomes the registered alias."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ".claude-plugin/marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="addy-agent-skills",
            plugins=(MarketplacePlugin(name="agent-skills"),),
        )

        result = runner.invoke(marketplace, ["add", "addyosmani/agent-skills"])
        assert result.exit_code == 0
        # Registered source carries the manifest's name, not the repo name.
        registered_source = mock_add.call_args[0][0]
        assert registered_source.name == "addy-agent-skills"
        assert registered_source.repo == "agent-skills"
        # Install hint surfaces the alias the user must use next.
        assert "apm install <plugin>@addy-agent-skills" in result.output

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_cli_name_overrides_manifest(self, mock_detect, mock_fetch, mock_add, runner):
        """An explicit --name flag wins over the manifest's name."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="manifest-alias",
            plugins=(MarketplacePlugin(name="p1"),),
        )

        result = runner.invoke(marketplace, ["add", "acme/plugins", "--name", "custom-alias"])
        assert result.exit_code == 0
        registered_source = mock_add.call_args[0][0]
        assert registered_source.name == "custom-alias"
        # No install hint when the user explicitly chose the alias.
        assert "Install plugins with" not in result.output

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_falls_back_when_manifest_name_invalid(
        self, mock_detect, mock_fetch, mock_add, runner
    ):
        """Invalid manifest.name triggers a soft fallback to the repo name."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="has spaces!",
            plugins=(MarketplacePlugin(name="p1"),),
        )

        result = runner.invoke(marketplace, ["add", "acme/plugins"])
        # Soft fallback: the command still succeeds.
        assert result.exit_code == 0
        registered_source = mock_add.call_args[0][0]
        assert registered_source.name == "plugins"
        # User sees a warning quoting the offending value.
        assert "has spaces!" in result.output
        assert "Falling back to repo name" in result.output

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_falls_back_when_manifest_name_missing(
        self, mock_detect, mock_fetch, mock_add, runner
    ):
        """Empty manifest.name silently falls back to the repo name."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="",
            plugins=(MarketplacePlugin(name="p1"),),
        )

        result = runner.invoke(marketplace, ["add", "acme/plugins"])
        assert result.exit_code == 0
        registered_source = mock_add.call_args[0][0]
        assert registered_source.name == "plugins"
        # No warning when the publisher simply omitted the field.
        assert "Falling back" not in result.output
        # No install hint either: alias matches the repo name -- predictable.
        assert "Install plugins with" not in result.output

    def test_add_rejects_invalid_cli_name(self, runner):
        """An invalid --name flag is a user error and hard-fails."""
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(marketplace, ["add", "acme/plugins", "--name", "bad name"])
        assert result.exit_code != 0
        assert "Invalid marketplace name" in result.output

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_awesome_copilot_pattern_unchanged(self, mock_detect, mock_fetch, mock_add, runner):
        """Regression: github/awesome-copilot manifest name == repo name -> no behaviour change."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ".github/plugin/marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="awesome-copilot",
            plugins=(MarketplacePlugin(name="azure-cloud-development"),),
        )

        result = runner.invoke(marketplace, ["add", "github/awesome-copilot"])
        assert result.exit_code == 0
        registered_source = mock_add.call_args[0][0]
        assert registered_source.name == "awesome-copilot"
        # Alias matches the repo name, so the install hint is suppressed.
        assert "Install plugins with" not in result.output

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_verbose_shows_alias_source(self, mock_detect, mock_fetch, runner):
        """Verbose mode reports which precedence tier picked the alias."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="acme-tools",
            plugins=(MarketplacePlugin(name="p1"),),
        )

        result = runner.invoke(marketplace, ["add", "acme/plugins", "--verbose"])
        assert result.exit_code == 0
        assert "Alias source: manifest.name" in result.output

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_successful_add(self, mock_detect, mock_fetch, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="Test",
            plugins=(MarketplacePlugin(name="p1"),),
        )

        result = runner.invoke(marketplace, ["add", "acme-org/plugins"])
        assert result.exit_code == 0
        assert "registered" in result.output.lower() or "1 plugin" in result.output

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_respects_github_host(self, mock_detect, mock_fetch, runner, monkeypatch):
        from apm_cli.commands.marketplace import marketplace

        monkeypatch.setenv("GITHUB_HOST", "ghe.corp.example.com")
        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="Test",
            plugins=(MarketplacePlugin(name="p1"),),
        )

        result = runner.invoke(marketplace, ["add", "acme-org/plugins"])
        assert result.exit_code == 0

        # The probe source passed to _auto_detect_path should carry the GHE host
        probe_source = mock_detect.call_args[0][0]
        assert probe_source.host == "ghe.corp.example.com"

        # The final source passed to fetch_marketplace should also carry it
        final_source = mock_fetch.call_args[0][0]
        assert final_source.host == "ghe.corp.example.com"

    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_no_marketplace_json_found(self, mock_detect, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = None
        result = runner.invoke(marketplace, ["add", "acme-org/empty-repo"])
        assert result.exit_code != 0
        assert "marketplace.json" in result.output


class TestMarketplaceList:
    """marketplace list."""

    def test_empty_list(self, runner):
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(marketplace, ["list"])
        assert result.exit_code == 0
        assert "no marketplace" in result.output.lower() or "add" in result.output.lower()

    @patch("apm_cli.marketplace.registry.get_registered_marketplaces")
    def test_list_with_entries(self, mock_get, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = [
            MarketplaceSource(name="acme", owner="acme-org", repo="plugins"),
        ]
        result = runner.invoke(marketplace, ["list"])
        assert result.exit_code == 0
        assert "acme" in result.output


class TestMarketplaceBrowse:
    """marketplace browse NAME."""

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_browse_shows_plugins(self, mock_get, mock_fetch, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch.return_value = MarketplaceManifest(
            name="Acme",
            plugins=(
                MarketplacePlugin(name="security-checks", description="Scans"),
                MarketplacePlugin(name="code-review", description="Reviews"),
            ),
        )
        result = runner.invoke(marketplace, ["browse", "acme"])
        assert result.exit_code == 0
        assert "security-checks" in result.output


class TestMarketplaceUpdate:
    """marketplace update [NAME]."""

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client.clear_marketplace_cache")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_update_single(self, mock_get, mock_clear, mock_fetch, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch.return_value = MarketplaceManifest(
            name="Acme", plugins=(MarketplacePlugin(name="p1"),)
        )
        result = runner.invoke(marketplace, ["update", "acme"])
        assert result.exit_code == 0
        assert "updated" in result.output.lower() or "1 plugin" in result.output


class TestMarketplaceRemove:
    """marketplace remove NAME."""

    @patch("apm_cli.marketplace.client.clear_marketplace_cache")
    @patch("apm_cli.marketplace.registry.remove_marketplace")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_remove_with_confirm(self, mock_get, mock_remove, mock_clear, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        result = runner.invoke(marketplace, ["remove", "acme", "--yes"])
        assert result.exit_code == 0
        mock_remove.assert_called_once()
        assert "removed" in result.output.lower()


class TestSearch:
    """Top-level search command -- requires QUERY@MARKETPLACE format."""

    def test_search_missing_at_symbol(self, runner):
        from apm_cli.commands.marketplace import search

        result = runner.invoke(search, ["security"])
        assert result.exit_code != 0
        assert "QUERY@MARKETPLACE" in result.output

    def test_search_empty_query(self, runner):
        from apm_cli.commands.marketplace import search

        result = runner.invoke(search, ["@skills"])
        assert result.exit_code != 0
        assert "QUERY" in result.output and "MARKETPLACE" in result.output

    def test_search_empty_marketplace(self, runner):
        from apm_cli.commands.marketplace import search

        result = runner.invoke(search, ["security@"])
        assert result.exit_code != 0
        assert "QUERY" in result.output and "MARKETPLACE" in result.output

    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_search_unknown_marketplace(self, mock_get, runner):
        from apm_cli.commands.marketplace import search
        from apm_cli.marketplace.errors import MarketplaceNotFoundError

        mock_get.side_effect = MarketplaceNotFoundError("nonexistent")
        result = runner.invoke(search, ["security@nonexistent"])
        assert result.exit_code != 0
        assert "not registered" in result.output.lower()

    @patch("apm_cli.marketplace.client.search_marketplace")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_search_finds_results(self, mock_get, mock_search, runner):
        from apm_cli.commands.marketplace import search

        mock_get.return_value = MarketplaceSource(
            name="skills",
            owner="anthropics",
            repo="anthropics/skills",
            path=".claude-plugin/marketplace.json",
        )
        mock_search.return_value = [
            MarketplacePlugin(
                name="security-scanner",
                description="Scans code",
                source_marketplace="skills",
            ),
        ]
        result = runner.invoke(search, ["security@skills"])
        assert result.exit_code == 0
        assert "security-scanner" in result.output

    @patch("apm_cli.marketplace.client.search_marketplace")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_search_no_results(self, mock_get, mock_search, runner):
        from apm_cli.commands.marketplace import search

        mock_get.return_value = MarketplaceSource(
            name="skills",
            owner="anthropics",
            repo="anthropics/skills",
            path=".claude-plugin/marketplace.json",
        )
        mock_search.return_value = []
        result = runner.invoke(search, ["zzz-nonexistent@skills"])
        assert result.exit_code == 0
        assert (
            "no plugin" in result.output.lower()
            or "not found" in result.output.lower()
            or "browse" in result.output.lower()
        )
