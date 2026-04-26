"""Tests for marketplace data models and JSON parser."""

import pytest

from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
    parse_marketplace_json,
)


class TestMarketplaceSource:
    """Frozen dataclass for registered marketplace sources."""

    def test_basic_creation(self):
        src = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        assert src.name == "acme"
        assert src.owner == "acme-org"
        assert src.repo == "plugins"
        assert src.host == "github.com"
        assert src.branch == "main"
        assert src.path == "marketplace.json"

    def test_frozen(self):
        src = MarketplaceSource(name="x", owner="o", repo="r")
        with pytest.raises(AttributeError):
            src.name = "y"

    def test_to_dict_defaults(self):
        src = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        d = src.to_dict()
        assert d == {"name": "acme", "owner": "acme-org", "repo": "plugins"}
        assert "host" not in d  # default omitted
        assert "branch" not in d

    def test_to_dict_non_defaults(self):
        src = MarketplaceSource(
            name="acme",
            owner="acme-org",
            repo="plugins",
            host="ghe.corp.com",
            branch="release",
            path=".github/plugin/marketplace.json",
        )
        d = src.to_dict()
        assert d["host"] == "ghe.corp.com"
        assert d["branch"] == "release"
        assert d["path"] == ".github/plugin/marketplace.json"

    def test_from_dict_minimal(self):
        src = MarketplaceSource.from_dict(
            {"name": "acme", "owner": "acme-org", "repo": "plugins"}
        )
        assert src.name == "acme"
        assert src.host == "github.com"

    def test_from_dict_full(self):
        src = MarketplaceSource.from_dict(
            {
                "name": "acme",
                "owner": "acme-org",
                "repo": "plugins",
                "host": "ghe.corp.com",
                "branch": "release",
                "path": ".claude-plugin/marketplace.json",
            }
        )
        assert src.host == "ghe.corp.com"
        assert src.branch == "release"
        assert src.path == ".claude-plugin/marketplace.json"

    def test_roundtrip(self):
        original = MarketplaceSource(
            name="acme",
            owner="acme-org",
            repo="plugins",
            host="ghe.corp.com",
            branch="release",
        )
        restored = MarketplaceSource.from_dict(original.to_dict())
        assert restored == original


class TestMarketplacePlugin:
    """Frozen dataclass for plugin entries."""

    def test_basic_creation(self):
        p = MarketplacePlugin(name="my-plugin", description="A plugin")
        assert p.name == "my-plugin"
        assert p.description == "A plugin"
        assert p.tags == ()
        assert p.source is None

    def test_frozen(self):
        p = MarketplacePlugin(name="x")
        with pytest.raises(AttributeError):
            p.name = "y"

    def test_matches_query_name(self):
        p = MarketplacePlugin(name="security-checks", description="Scan for vulns")
        assert p.matches_query("security")
        assert p.matches_query("SECURITY")

    def test_matches_query_description(self):
        p = MarketplacePlugin(name="x", description="Scan for vulnerabilities")
        assert p.matches_query("vuln")

    def test_matches_query_tags(self):
        p = MarketplacePlugin(name="x", tags=("security", "audit"))
        assert p.matches_query("audit")

    def test_no_match(self):
        p = MarketplacePlugin(name="x", description="desc", tags=("a",))
        assert not p.matches_query("zzz")


class TestMarketplaceManifest:
    """Frozen dataclass for parsed marketplace content."""

    def test_find_plugin(self):
        plugins = (
            MarketplacePlugin(name="alpha"),
            MarketplacePlugin(name="beta"),
        )
        m = MarketplaceManifest(name="test", plugins=plugins)
        assert m.find_plugin("alpha").name == "alpha"
        assert m.find_plugin("BETA").name == "beta"
        assert m.find_plugin("gamma") is None

    def test_search(self):
        plugins = (
            MarketplacePlugin(name="security-scanner", description="Scans stuff"),
            MarketplacePlugin(name="code-formatter", description="Formats code"),
        )
        m = MarketplaceManifest(name="test", plugins=plugins)
        results = m.search("security")
        assert len(results) == 1
        assert results[0].name == "security-scanner"


class TestParseMarketplaceJson:
    """Parser for both Copilot CLI and Claude Code formats."""

    def test_copilot_format(self):
        data = {
            "name": "Acme Tools",
            "description": "Corporate plugins",
            "plugins": [
                {
                    "name": "security-checks",
                    "description": "Security scanning",
                    "repository": "acme-org/security-plugin",
                    "ref": "v1.3.0",
                },
                {
                    "name": "code-review",
                    "description": "Code review helper",
                    "repository": "acme-org/review-plugin",
                },
            ],
        }
        manifest = parse_marketplace_json(data, "acme-tools")
        assert manifest.name == "Acme Tools"
        assert manifest.description == "Corporate plugins"
        assert len(manifest.plugins) == 2
        p1 = manifest.find_plugin("security-checks")
        assert p1.source == {"type": "github", "repo": "acme-org/security-plugin", "ref": "v1.3.0"}
        p2 = manifest.find_plugin("code-review")
        assert p2.source == {"type": "github", "repo": "acme-org/review-plugin"}

    def test_claude_format_github(self):
        data = {
            "name": "Claude Plugins",
            "plugins": [
                {
                    "name": "my-plugin",
                    "description": "A plugin",
                    "source": {
                        "type": "github",
                        "repo": "owner/plugin-repo",
                        "ref": "v2.0",
                    },
                }
            ],
        }
        manifest = parse_marketplace_json(data, "claude-mkt")
        assert len(manifest.plugins) == 1
        p = manifest.plugins[0]
        assert p.name == "my-plugin"
        assert p.source["type"] == "github"
        assert p.source["repo"] == "owner/plugin-repo"
        assert p.source_marketplace == "claude-mkt"

    def test_claude_format_relative(self):
        data = {
            "name": "Test",
            "plugins": [
                {"name": "local-plugin", "source": "./plugins/local"},
            ],
        }
        manifest = parse_marketplace_json(data)
        assert len(manifest.plugins) == 1
        assert manifest.plugins[0].source == "./plugins/local"

    def test_claude_format_url(self):
        data = {
            "name": "Test",
            "plugins": [
                {
                    "name": "url-plugin",
                    "source": {"type": "url", "url": "https://github.com/org/repo"},
                }
            ],
        }
        manifest = parse_marketplace_json(data)
        assert len(manifest.plugins) == 1
        assert manifest.plugins[0].source["type"] == "url"

    def test_claude_format_git_subdir(self):
        data = {
            "name": "Test",
            "plugins": [
                {
                    "name": "subdir-plugin",
                    "source": {
                        "type": "git-subdir",
                        "repo": "owner/monorepo",
                        "subdir": "packages/plugin-a",
                        "ref": "main",
                    },
                }
            ],
        }
        manifest = parse_marketplace_json(data)
        assert len(manifest.plugins) == 1
        assert manifest.plugins[0].source["type"] == "git-subdir"

    def test_npm_source_skipped(self):
        data = {
            "name": "Test",
            "plugins": [
                {
                    "name": "npm-plugin",
                    "source": {"type": "npm", "package": "@scope/pkg"},
                }
            ],
        }
        manifest = parse_marketplace_json(data)
        assert len(manifest.plugins) == 0

    def test_copilot_cli_source_key_as_type(self):
        """Copilot CLI format uses 'source' (not 'type') as type discriminator inside dict."""
        data = {
            "name": "Awesome Copilot",
            "plugins": [
                {
                    "name": "azure",
                    "description": "Azure skills",
                    "source": {
                        "source": "github",
                        "repo": "microsoft/azure-skills",
                        "path": ".github/plugins/azure-skills",
                    },
                }
            ],
        }
        manifest = parse_marketplace_json(data, "awesome-copilot")
        assert len(manifest.plugins) == 1
        p = manifest.plugins[0]
        assert p.name == "azure"
        # Parser should normalize "source" key to "type" key
        assert p.source["type"] == "github"
        assert p.source["repo"] == "microsoft/azure-skills"
        assert p.source["path"] == ".github/plugins/azure-skills"

    def test_npm_via_source_key_skipped(self):
        """npm source type should be skipped even when using 'source' key."""
        data = {
            "name": "Test",
            "plugins": [
                {
                    "name": "npm-plugin",
                    "source": {"source": "npm", "package": "@scope/pkg"},
                }
            ],
        }
        manifest = parse_marketplace_json(data)
        assert len(manifest.plugins) == 0

    def test_invalid_entries_skipped(self):
        data = {
            "name": "Test",
            "plugins": [
                {"name": "valid", "repository": "o/r"},
                {"description": "no name"},  # Missing name
                "not-a-dict",  # Non-dict
                {"name": "no-source"},  # Missing source
            ],
        }
        manifest = parse_marketplace_json(data)
        assert len(manifest.plugins) == 1
        assert manifest.plugins[0].name == "valid"

    def test_empty_plugins_list(self):
        data = {"name": "Empty"}
        manifest = parse_marketplace_json(data)
        assert len(manifest.plugins) == 0

    def test_plugins_not_a_list(self):
        data = {"name": "Bad", "plugins": "not-a-list"}
        manifest = parse_marketplace_json(data)
        assert len(manifest.plugins) == 0

    def test_tags_preserved(self):
        data = {
            "name": "Test",
            "plugins": [
                {
                    "name": "tagged",
                    "repository": "o/r",
                    "tags": ["security", "compliance"],
                }
            ],
        }
        manifest = parse_marketplace_json(data)
        assert manifest.plugins[0].tags == ("security", "compliance")

    def test_version_preserved(self):
        data = {
            "name": "Test",
            "plugins": [
                {"name": "versioned", "repository": "o/r", "version": "1.2.3"},
            ],
        }
        manifest = parse_marketplace_json(data)
        assert manifest.plugins[0].version == "1.2.3"

    def test_owner_string(self):
        """Owner can be a string (not a dict)."""
        data = {"name": "Test", "owner": "John Doe", "plugins": []}
        manifest = parse_marketplace_json(data)
        assert manifest.owner_name == "John Doe"

    def test_owner_dict(self):
        """Owner can be a dict with 'name' key."""
        data = {"name": "Test", "owner": {"name": "Jane"}, "plugins": []}
        manifest = parse_marketplace_json(data)
        assert manifest.owner_name == "Jane"

    def test_plugin_root_from_metadata(self):
        """metadata.pluginRoot is parsed into manifest.plugin_root."""
        data = {
            "name": "Test",
            "metadata": {"pluginRoot": "./plugins"},
            "plugins": [],
        }
        manifest = parse_marketplace_json(data)
        assert manifest.plugin_root == "./plugins"

    def test_plugin_root_missing_metadata(self):
        """No metadata section -> plugin_root is empty."""
        data = {"name": "Test", "plugins": []}
        manifest = parse_marketplace_json(data)
        assert manifest.plugin_root == ""

    def test_plugin_root_missing_key(self):
        """metadata present but no pluginRoot -> plugin_root is empty."""
        data = {"name": "Test", "metadata": {"version": "1.0"}, "plugins": []}
        manifest = parse_marketplace_json(data)
        assert manifest.plugin_root == ""


