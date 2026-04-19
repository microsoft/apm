"""Tests for URL-based marketplace add command.

Covers: URL detection, .well-known auto-resolution, name derivation,
HTTPS enforcement, fetch wiring, and GitHub regression guard.
Tests are kept in a separate file from test_marketplace_commands.py
which covers GitHub/OWNER/REPO sources only.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Redirect all filesystem state to a temp dir."""
    config_dir = str(tmp_path / ".apm")
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr(
        "apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json")
    )
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)


@pytest.fixture
def mock_url_manifest():
    return MarketplaceManifest(
        name="example-skills",
        plugins=(
            MarketplacePlugin(name="code-review", description="Reviews code"),
            MarketplacePlugin(name="security-scan", description="Scans"),
        ),
    )


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestURLDetection:
    """add command detects https:// arguments and routes to URL path."""

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    def test_https_url_not_rejected_as_invalid_format(
        self, mock_fetch, mock_add, runner, mock_url_manifest
    ):
        """https:// argument must NOT produce 'Invalid format' error."""
        from apm_cli.commands.marketplace import marketplace

        mock_fetch.return_value = mock_url_manifest
        result = runner.invoke(
            marketplace,
            ["add", "https://example.com/.well-known/agent-skills/index.json"],
        )
        assert "Invalid format" not in result.output
        assert "OWNER/REPO" not in result.output

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    def test_url_source_type_is_url(
        self, mock_fetch, mock_add, runner, mock_url_manifest
    ):
        """Source passed to add_marketplace must have source_type='url'."""
        from apm_cli.commands.marketplace import marketplace

        mock_fetch.return_value = mock_url_manifest
        runner.invoke(
            marketplace,
            ["add", "https://example.com/.well-known/agent-skills/index.json"],
        )
        assert mock_add.called
        source = mock_add.call_args[0][0]
        assert source.source_type == "url"

    def test_http_url_rejected(self, runner):
        """Plain http:// (non-TLS) must be rejected -- RFC requires HTTPS."""
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(marketplace, ["add", "http://example.com"])
        assert result.exit_code != 0
        assert "https" in result.output.lower()

    @pytest.mark.parametrize("url", [
        "HTTPS://EXAMPLE.COM",
        "Https://example.com",
        "HTTP://example.com",
    ])
    def test_mixed_case_scheme_detected_as_url(self, runner, url):
        """URL detection must be case-insensitive per RFC 3986 Section 3.1."""
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(marketplace, ["add", url])
        # Should NOT produce "Invalid format ... OWNER/REPO" --
        # it should enter the URL path (may fail on http or succeed on https)
        assert "OWNER/REPO" not in result.output


# ---------------------------------------------------------------------------
# .well-known auto-resolution
# ---------------------------------------------------------------------------


class TestWellKnownResolution:
    """Bare origin URLs are automatically resolved to the .well-known path."""

    @pytest.mark.parametrize("input_url,expected_url", [
        ("https://example.com",
         "https://example.com/.well-known/agent-skills/index.json"),
        ("https://example.com/",
         "https://example.com/.well-known/agent-skills/index.json"),
        ("https://example.com/.well-known/agent-skills/index.json",
         "https://example.com/.well-known/agent-skills/index.json"),
        ("https://example.com/.well-known/agent-skills/",
         "https://example.com/.well-known/agent-skills/index.json"),
        ("https://example.com/.well-known/agent-skills",
         "https://example.com/.well-known/agent-skills/index.json"),
        ("https://example.com?token=abc",
         "https://example.com/.well-known/agent-skills/index.json?token=abc"),
    ])
    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    def test_well_known_url_resolution(
        self, mock_fetch, mock_add, runner, mock_url_manifest,
        input_url, expected_url
    ):
        from apm_cli.commands.marketplace import marketplace

        mock_fetch.return_value = mock_url_manifest
        runner.invoke(marketplace, ["add", input_url])
        source = mock_add.call_args[0][0]
        assert source.url == expected_url


# ---------------------------------------------------------------------------
# Display name derivation
# ---------------------------------------------------------------------------


class TestDisplayNameDerivation:
    """Display name comes from --name or is derived from the URL hostname."""

    @pytest.mark.parametrize("args,expected_name", [
        (["add", "https://skills.example.com"], "skills.example.com"),
        (["add", "https://example.com", "--name", "my-skills"], "my-skills"),
    ])
    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    def test_name_resolved_correctly(
        self, mock_fetch, mock_add, runner, mock_url_manifest, args, expected_name
    ):
        from apm_cli.commands.marketplace import marketplace

        mock_fetch.return_value = mock_url_manifest
        runner.invoke(marketplace, args)
        source = mock_add.call_args[0][0]
        assert source.name == expected_name

    def test_invalid_name_rejected(self, runner):
        """Names with spaces or special chars are rejected."""
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(
            marketplace,
            ["add", "https://example.com", "--name", "bad name!"],
        )
        assert result.exit_code != 0
        assert "invalid" in result.output.lower() or "name" in result.output.lower()


# ---------------------------------------------------------------------------
# Fetch wiring
# ---------------------------------------------------------------------------


class TestFetchWiring:
    """fetch_marketplace and add_marketplace are called correctly."""

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    def test_fetch_marketplace_called_with_url_source(
        self, mock_fetch, mock_add, runner, mock_url_manifest
    ):
        """fetch_marketplace must be called with a URL-typed source."""
        from apm_cli.commands.marketplace import marketplace

        mock_fetch.return_value = mock_url_manifest
        result = runner.invoke(
            marketplace,
            ["add", "https://example.com/.well-known/agent-skills/index.json"],
        )
        assert result.exit_code == 0
        assert mock_fetch.called
        source = mock_fetch.call_args[0][0]
        assert source.source_type == "url"

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    def test_add_marketplace_called_on_success(
        self, mock_fetch, mock_add, runner, mock_url_manifest
    ):
        """add_marketplace must be called when fetch succeeds."""
        from apm_cli.commands.marketplace import marketplace

        mock_fetch.return_value = mock_url_manifest
        result = runner.invoke(marketplace, ["add", "https://example.com"])
        assert result.exit_code == 0
        assert mock_add.called

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    def test_success_message_shown(
        self, mock_fetch, mock_add, runner, mock_url_manifest
    ):
        """Success output must mention the marketplace name and skill count."""
        from apm_cli.commands.marketplace import marketplace

        mock_fetch.return_value = mock_url_manifest
        result = runner.invoke(
            marketplace,
            ["add", "https://example.com", "--name", "my-skills"],
        )
        assert result.exit_code == 0
        assert "my-skills" in result.output
        assert "2" in result.output  # 2 plugins in mock manifest

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    def test_fetch_failure_exits_with_error(self, mock_fetch, runner):
        """If fetch raises, command exits non-zero with an error message."""
        from apm_cli.commands.marketplace import marketplace

        mock_fetch.side_effect = Exception("connection refused")
        result = runner.invoke(marketplace, ["add", "https://example.com"])
        assert result.exit_code != 0
        assert "failed" in result.output.lower() or "error" in result.output.lower()


# ---------------------------------------------------------------------------
# GitHub regression guard
# ---------------------------------------------------------------------------


class TestGitHubRegression:
    """Existing OWNER/REPO path must be completely unaffected."""

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_owner_repo_still_works(
        self, mock_detect, mock_fetch, mock_add, runner
    ):
        """OWNER/REPO argument routes to GitHub path, not URL path."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="Test", plugins=(MarketplacePlugin(name="p1"),)
        )
        result = runner.invoke(marketplace, ["add", "acme-org/plugins"])
        assert result.exit_code == 0
        source = mock_add.call_args[0][0]
        assert source.source_type == "github"
        assert source.owner == "acme-org"
        assert source.repo == "plugins"

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_owner_repo_does_not_call_url_path(
        self, mock_detect, mock_fetch, mock_add, runner
    ):
        """_auto_detect_path must still be called for GitHub sources."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="Test", plugins=(MarketplacePlugin(name="p1"),)
        )
        runner.invoke(marketplace, ["add", "acme-org/plugins"])
        assert mock_detect.called


# ---------------------------------------------------------------------------
# list command -- URL source display (t8-test-07)
# ---------------------------------------------------------------------------


class TestListURLSource:
    """list command must show URL for URL-based sources, not owner/repo."""

    def _register_url_source(self, config_dir):
        """Write a URL source into the marketplaces registry file."""
        import json, os
        os.makedirs(config_dir, exist_ok=True)
        registry = os.path.join(config_dir, "marketplaces.json")
        with open(registry, "w") as f:
            json.dump(
                {"marketplaces": [{"name": "example-skills", "source_type": "url",
                  "url": "https://example.com/.well-known/agent-skills/index.json"}]},
                f,
            )

    def test_list_plaintext_shows_url_not_owner_repo(self, runner, tmp_path, monkeypatch):
        from apm_cli.commands.marketplace import marketplace

        config_dir = str(tmp_path / ".apm")
        monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
        monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
        monkeypatch.setattr("apm_cli.config._config_cache", None)
        monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
        self._register_url_source(config_dir)

        # Force plain output (no Rich console) by patching _get_console to return None
        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            result = runner.invoke(marketplace, ["list"])

        url = "https://example.com/.well-known/agent-skills/index.json"
        assert url in result.output
        # Must not show "(/)"-style owner/repo for a URL source
        assert "(/" not in result.output

    def test_list_does_not_show_default_github_fields_for_url_source(
        self, runner, tmp_path, monkeypatch
    ):
        from apm_cli.commands.marketplace import marketplace

        config_dir = str(tmp_path / ".apm")
        monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
        monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
        monkeypatch.setattr("apm_cli.config._config_cache", None)
        monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
        self._register_url_source(config_dir)

        with patch("apm_cli.commands.marketplace._get_console", return_value=None):
            result = runner.invoke(marketplace, ["list"])

        # Default GitHub placeholder values must not bleed through
        assert "marketplace.json" not in result.output
        assert "main" not in result.output


# ---------------------------------------------------------------------------
# remove command -- URL source display (t8-test-08, t8-test-09)
# ---------------------------------------------------------------------------


class TestRemoveURLSource:
    """remove command must show URL in confirmation and clear correct cache."""

    def _register_url_source(self, config_dir):
        import json, os
        os.makedirs(config_dir, exist_ok=True)
        registry = os.path.join(config_dir, "marketplaces.json")
        with open(registry, "w") as f:
            json.dump(
                {"marketplaces": [{"name": "example-skills", "source_type": "url",
                  "url": "https://example.com/.well-known/agent-skills/index.json"}]},
                f,
            )

    def test_remove_confirmation_shows_url(self, runner, tmp_path, monkeypatch):
        from apm_cli.commands.marketplace import marketplace

        config_dir = str(tmp_path / ".apm")
        monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
        monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
        monkeypatch.setattr("apm_cli.config._config_cache", None)
        monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
        self._register_url_source(config_dir)

        # Provide "n" so it cancels without actually removing
        result = runner.invoke(marketplace, ["remove", "example-skills"], input="n\n")

        url = "https://example.com/.well-known/agent-skills/index.json"
        assert url in result.output
        # Confirm text must not show default "(/)"-style placeholder
        assert "(/" not in result.output

    def test_remove_yes_clears_url_cache_key(self, runner, tmp_path, monkeypatch):
        """--yes must clear the sha256-based cache slot, not a host-based one."""
        import hashlib, json, os, time
        from apm_cli.marketplace.client import _cache_data_path, _cache_meta_path

        config_dir = str(tmp_path / ".apm")
        monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
        monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
        monkeypatch.setattr("apm_cli.config._config_cache", None)
        monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
        self._register_url_source(config_dir)

        # Write a cache file at the correct sha256-based key
        url = "https://example.com/.well-known/agent-skills/index.json"
        cache_key = hashlib.sha256(url.encode()).hexdigest()[:16]
        cache_dir = os.path.join(config_dir, "cache", "marketplace")
        os.makedirs(cache_dir, exist_ok=True)
        data_path = _cache_data_path(cache_key)
        meta_path = _cache_meta_path(cache_key)
        with open(data_path, "w") as f:
            json.dump({"skills": []}, f)
        with open(meta_path, "w") as f:
            json.dump({"fetched_at": time.time(), "ttl_seconds": 3600}, f)

        from apm_cli.commands.marketplace import marketplace
        result = runner.invoke(marketplace, ["remove", "--yes", "example-skills"])

        assert result.exit_code == 0
        assert not os.path.exists(data_path), "Cache data file must be removed"
        assert not os.path.exists(meta_path), "Cache meta file must be removed"



# ---------------------------------------------------------------------------
# E1 / T9 / T10: targeted exception messages in marketplace add
# ---------------------------------------------------------------------------


class TestAddCommandErrorMessages:
    """marketplace add must produce targeted error messages, not generic wrapping."""

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    def test_schema_error_shows_invalid_index_format(self, mock_fetch, runner):
        """T9: wrong $schema -> 'Invalid index format', not generic 'Failed to register'."""
        from apm_cli.commands.marketplace import marketplace

        mock_fetch.side_effect = ValueError(
            "Unrecognized or missing Agent Skills index $schema: 'bad-schema'"
        )
        result = runner.invoke(marketplace, ["add", "https://example.com/skills.json"])
        assert result.exit_code == 1
        assert "Failed to register marketplace" not in result.output
        assert "schema" in result.output.lower() or "index format" in result.output.lower()

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    def test_fetch_error_not_double_wrapped(self, mock_fetch, runner):
        """T10: MarketplaceFetchError message not wrapped in 'Failed to register marketplace:'."""
        from apm_cli.commands.marketplace import marketplace
        from apm_cli.marketplace.errors import MarketplaceFetchError

        mock_fetch.side_effect = MarketplaceFetchError(
            "example.com", "Unrecognized index format at 'https://example.com/'"
        )
        result = runner.invoke(marketplace, ["add", "https://example.com/skills.json"])
        assert result.exit_code == 1
        assert "Failed to register marketplace" not in result.output
        assert "Failed to fetch marketplace" in result.output


# ---------------------------------------------------------------------------
# update command -- URL source cache clearing
# ---------------------------------------------------------------------------


class TestUpdateURLSource:
    """marketplace update must clear the correct (SHA256-based) cache for URL sources."""

    def _make_url_source(self, name="example-skills"):
        return MarketplaceSource(
            name=name,
            source_type="url",
            url="https://example.com/.well-known/agent-skills/index.json",
        )

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client.clear_marketplace_cache")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_update_single_url_source_passes_source_to_clear(
        self, mock_get, mock_clear, mock_fetch, runner
    ):
        """Single-name update must call clear_marketplace_cache(source=<source>)."""
        from apm_cli.commands.marketplace import marketplace

        url_source = self._make_url_source()
        mock_get.return_value = url_source
        mock_fetch.return_value = MarketplaceManifest(
            name="example-skills", plugins=()
        )
        runner.invoke(marketplace, ["update", "example-skills"])
        mock_clear.assert_called_once()
        _, kwargs = mock_clear.call_args
        assert kwargs.get("source") == url_source, (
            "clear_marketplace_cache must receive source= for URL sources, "
            "not name+host which generates a wrong cache key"
        )

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client.clear_marketplace_cache")
    @patch("apm_cli.marketplace.registry.get_registered_marketplaces")
    def test_update_all_url_sources_passes_source_to_clear(
        self, mock_list, mock_clear, mock_fetch, runner
    ):
        """Bulk update must call clear_marketplace_cache(source=s) for URL sources."""
        from apm_cli.commands.marketplace import marketplace

        url_source = self._make_url_source()
        mock_list.return_value = [url_source]
        mock_fetch.return_value = MarketplaceManifest(
            name="example-skills", plugins=()
        )
        runner.invoke(marketplace, ["update"])
        mock_clear.assert_called_once()
        _, kwargs = mock_clear.call_args
        assert kwargs.get("source") == url_source, (
            "clear_marketplace_cache must receive source= for URL sources"
        )
