"""Tests for marketplace resolver -- regex and source type resolution."""

import pytest

from apm_cli.marketplace.models import MarketplacePlugin
from apm_cli.marketplace.resolver import (
    _resolve_git_subdir_source,
    _resolve_github_source,
    _resolve_relative_source,
    _resolve_url_source,
    parse_marketplace_ref,
    resolve_plugin_source,
)


class TestParseMarketplaceRef:
    """Regex positive/negative cases for NAME@MARKETPLACE detection."""

    # Positive cases -- valid marketplace refs
    def test_simple(self):
        assert parse_marketplace_ref("security-checks@acme-tools") == (
            "security-checks",
            "acme-tools",
            None,
        )

    def test_dots(self):
        assert parse_marketplace_ref("my.plugin@my.marketplace") == (
            "my.plugin",
            "my.marketplace",
            None,
        )

    def test_underscores(self):
        assert parse_marketplace_ref("my_plugin@my_marketplace") == (
            "my_plugin",
            "my_marketplace",
            None,
        )

    def test_mixed(self):
        assert parse_marketplace_ref("plugin-v2.0@corp_tools") == (
            "plugin-v2.0",
            "corp_tools",
            None,
        )

    def test_whitespace_stripped(self):
        assert parse_marketplace_ref("  name@mkt  ") == ("name", "mkt", None)

    # Negative cases -- not marketplace refs (should return None)
    def test_owner_repo(self):
        """owner/repo has slash -> rejected."""
        assert parse_marketplace_ref("owner/repo") is None

    def test_owner_repo_at_alias(self):
        """owner/repo@alias has slash -> rejected."""
        assert parse_marketplace_ref("owner/repo@alias") is None

    def test_ssh_url(self):
        """git@host:... has colon -> rejected."""
        assert parse_marketplace_ref("git@github.com:o/r") is None

    def test_https_url(self):
        """https://... has slashes -> rejected."""
        assert parse_marketplace_ref("https://github.com/o/r") is None

    def test_no_at(self):
        """Bare name without @ is NOT a marketplace ref."""
        assert parse_marketplace_ref("just-a-name") is None

    def test_empty(self):
        assert parse_marketplace_ref("") is None

    def test_only_at(self):
        """Just @ with no name/marketplace."""
        assert parse_marketplace_ref("@") is None

    def test_at_prefix(self):
        """@marketplace with no name."""
        assert parse_marketplace_ref("@mkt") is None

    def test_at_suffix(self):
        """name@ with no marketplace."""
        assert parse_marketplace_ref("name@") is None

    def test_multiple_at(self):
        """Multiple @ signs."""
        assert parse_marketplace_ref("a@b@c") is None

    def test_special_chars(self):
        """Special characters that aren't in the allowed set."""
        assert parse_marketplace_ref("name@mkt!") is None
        assert parse_marketplace_ref("na me@mkt") is None


class TestResolveGithubSource:
    """Resolve github source type."""

    def test_with_ref(self):
        assert _resolve_github_source({"repo": "owner/repo", "ref": "v1.0"}) == "owner/repo#v1.0"

    def test_without_ref(self):
        assert _resolve_github_source({"repo": "owner/repo"}) == "owner/repo"

    def test_with_path(self):
        """Copilot CLI format uses 'path' for subdirectory."""
        result = _resolve_github_source(
            {
                "repo": "microsoft/azure-skills",
                "path": ".github/plugins/azure-skills",
            }
        )
        assert result == "microsoft/azure-skills/.github/plugins/azure-skills"

    def test_with_path_and_ref(self):
        result = _resolve_github_source(
            {
                "repo": "owner/mono",
                "path": "plugins/foo",
                "ref": "v2.0",
            }
        )
        assert result == "owner/mono/plugins/foo#v2.0"

    def test_path_traversal_rejected(self):
        with pytest.raises(ValueError, match="traversal sequence"):
            _resolve_github_source({"repo": "owner/repo", "path": "../escape"})

    def test_invalid_repo(self):
        with pytest.raises(ValueError, match="owner/repo"):
            _resolve_github_source({"repo": "just-a-name"})


class TestResolveUrlSource:
    """Resolve url source type."""

    def test_github_https(self):
        assert _resolve_url_source({"url": "https://github.com/owner/repo"}) == "owner/repo"

    def test_github_https_with_git_suffix(self):
        assert _resolve_url_source({"url": "https://github.com/owner/repo.git"}) == "owner/repo"

    def test_non_github_url(self):
        # DependencyReference.parse() handles any valid Git host URL
        assert _resolve_url_source({"url": "https://gitlab.com/owner/repo"}) == "owner/repo"

    def test_url_host_is_not_preserved_in_output(self):
        """Host from the URL is stripped -- only owner/repo is returned.

        This is intentional: downstream RefResolver resolves owner/repo
        against the configured GITHUB_HOST, not the URL's original host.
        Cross-host resolution is tracked in #1010.
        """
        # Different hosts all resolve to the same owner/repo coordinate
        urls = [
            "https://github.com/acme/tools",
            "https://gitlab.com/acme/tools",
            "https://bitbucket.org/acme/tools",
            "https://corp.ghe.com/acme/tools",
        ]
        for url in urls:
            result = _resolve_url_source({"url": url})
            assert result == "acme/tools", f"Expected 'acme/tools' for {url}, got '{result}'"

    def test_ghes_url(self):
        """GHES URLs are resolved via DependencyReference.parse()."""
        assert _resolve_url_source({"url": "https://corp.ghe.com/org/repo"}) == "org/repo"

    def test_ssh_url(self):
        """SSH URLs are resolved via DependencyReference.parse()."""
        assert _resolve_url_source({"url": "git@gitlab.com:org/repo.git"}) == "org/repo"

    def test_url_with_ref_fragment(self):
        """URL with #ref preserves the ref in owner/repo#ref format."""
        assert _resolve_url_source({"url": "https://github.com/org/repo#v2.0"}) == "org/repo#v2.0"

    def test_empty_url_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            _resolve_url_source({"url": ""})

    def test_local_path_rejected(self):
        with pytest.raises(ValueError, match="local path"):
            _resolve_url_source({"url": "./local/path"})

    def test_invalid_url_rejected(self):
        with pytest.raises(ValueError, match="Cannot resolve URL source"):
            _resolve_url_source({"url": ":::invalid:::"})


class TestResolveGitSubdirSource:
    """Resolve git-subdir source type."""

    def test_with_ref(self):
        result = _resolve_git_subdir_source(
            {
                "repo": "owner/monorepo",
                "subdir": "packages/plugin-a",
                "ref": "main",
            }
        )
        assert result == "owner/monorepo/packages/plugin-a#main"

    def test_without_ref(self):
        result = _resolve_git_subdir_source({"repo": "owner/monorepo"})
        assert result == "owner/monorepo"

    def test_without_subdir(self):
        result = _resolve_git_subdir_source({"repo": "owner/monorepo", "ref": "v1"})
        assert result == "owner/monorepo#v1"

    def test_invalid_repo(self):
        with pytest.raises(ValueError, match="owner/repo"):
            _resolve_git_subdir_source({"repo": "bad"})

    def test_path_traversal_rejected(self):
        with pytest.raises(ValueError, match="traversal sequence"):
            _resolve_git_subdir_source({"repo": "owner/mono", "subdir": "../escape"})


class TestResolveRelativeSource:
    """Resolve relative path source type."""

    def test_relative_path(self):
        result = _resolve_relative_source("./plugins/my-plugin", "acme-org", "marketplace")
        assert result == "acme-org/marketplace/plugins/my-plugin"

    def test_root_relative(self):
        result = _resolve_relative_source(".", "acme-org", "marketplace")
        assert result == "acme-org/marketplace"

    def test_path_traversal_rejected(self):
        with pytest.raises(ValueError, match="traversal sequence"):
            _resolve_relative_source("../escape", "acme-org", "marketplace")

    def test_bare_name_without_plugin_root(self):
        """Bare name without plugin_root resolves directly under repo."""
        result = _resolve_relative_source("my-plugin", "github", "awesome-copilot")
        assert result == "github/awesome-copilot/my-plugin"

    def test_bare_name_with_plugin_root(self):
        """Bare name with plugin_root gets prefixed."""
        result = _resolve_relative_source(
            "azure-cloud-development",
            "github",
            "awesome-copilot",
            plugin_root="./plugins",
        )
        assert result == "github/awesome-copilot/plugins/azure-cloud-development"

    def test_plugin_root_without_dot_slash(self):
        """plugin_root without leading ./ still works."""
        result = _resolve_relative_source(
            "my-plugin",
            "org",
            "repo",
            plugin_root="packages",
        )
        assert result == "org/repo/packages/my-plugin"

    def test_plugin_root_ignored_for_path_sources(self):
        """Sources with / are already paths -- plugin_root should not apply."""
        result = _resolve_relative_source(
            "./custom/path/plugin",
            "org",
            "repo",
            plugin_root="./plugins",
        )
        assert result == "org/repo/custom/path/plugin"

    def test_plugin_root_trailing_slashes(self):
        """Trailing slashes on plugin_root are normalized."""
        result = _resolve_relative_source(
            "my-plugin",
            "org",
            "repo",
            plugin_root="./plugins/",
        )
        assert result == "org/repo/plugins/my-plugin"

    def test_dot_source_with_plugin_root(self):
        """source='.' means repo root -- plugin_root must not apply."""
        result = _resolve_relative_source(
            ".",
            "org",
            "repo",
            plugin_root="./plugins",
        )
        assert result == "org/repo"


class TestResolvePluginSource:
    """Integration of all source type resolvers."""

    def test_github_source(self):
        p = MarketplacePlugin(
            name="test",
            source={"type": "github", "repo": "owner/repo", "ref": "v1.0"},
        )
        assert resolve_plugin_source(p) == "owner/repo#v1.0"

    def test_github_source_with_path(self):
        """Copilot CLI format: github source with 'path' field."""
        p = MarketplacePlugin(
            name="azure",
            source={
                "type": "github",
                "repo": "microsoft/azure-skills",
                "path": ".github/plugins/azure-skills",
            },
        )
        assert resolve_plugin_source(p) == "microsoft/azure-skills/.github/plugins/azure-skills"

    def test_url_source(self):
        p = MarketplacePlugin(
            name="test",
            source={"type": "url", "url": "https://github.com/owner/repo"},
        )
        assert resolve_plugin_source(p) == "owner/repo"

    def test_git_subdir_source(self):
        p = MarketplacePlugin(
            name="test",
            source={
                "type": "git-subdir",
                "repo": "owner/mono",
                "subdir": "pkg/a",
                "ref": "main",
            },
        )
        assert resolve_plugin_source(p) == "owner/mono/pkg/a#main"

    def test_relative_source(self):
        p = MarketplacePlugin(name="test", source="./plugins/local")
        assert resolve_plugin_source(p, "acme", "mkt") == "acme/mkt/plugins/local"

    def test_relative_bare_name_with_plugin_root(self):
        """Bare-name source with plugin_root gets prefixed (awesome-copilot pattern)."""
        p = MarketplacePlugin(name="azure-cloud-development", source="azure-cloud-development")
        result = resolve_plugin_source(p, "github", "awesome-copilot", plugin_root="./plugins")
        assert result == "github/awesome-copilot/plugins/azure-cloud-development"

    def test_npm_source_rejected(self):
        p = MarketplacePlugin(
            name="test",
            source={"type": "npm", "package": "@scope/pkg"},
        )
        with pytest.raises(ValueError, match="npm source type"):
            resolve_plugin_source(p)

    def test_unknown_source_type_rejected(self):
        p = MarketplacePlugin(
            name="test",
            source={"type": "unknown"},
        )
        with pytest.raises(ValueError, match="unsupported source type"):
            resolve_plugin_source(p)

    def test_no_source_rejected(self):
        p = MarketplacePlugin(name="test", source=None)
        with pytest.raises(ValueError, match="no source defined"):
            resolve_plugin_source(p)
