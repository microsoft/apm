"""Tests for marketplace resolver -- regex and source type resolution."""

from unittest.mock import patch

import pytest

from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.marketplace.resolver import (
    _resolve_git_subdir_source,
    _resolve_github_source,
    _resolve_relative_source,
    _resolve_url_source,
    parse_marketplace_ref,
    resolve_marketplace_plugin,
    resolve_plugin_source,
)
from apm_cli.models.dependency.reference import DependencyReference


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

    def test_repository_key_fallback(self):
        """Old marketplace format uses 'repository' instead of 'repo'."""
        assert (
            _resolve_github_source({"repository": "owner/repo", "ref": "v1.0"}) == "owner/repo#v1.0"
        )

    def test_repo_key_takes_precedence(self):
        """When both 'repo' and 'repository' are present, 'repo' wins."""
        result = _resolve_github_source(
            {"repo": "owner/new-repo", "repository": "owner/old-repo", "ref": "v1.0"}
        )
        assert result == "owner/new-repo#v1.0"


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

    def test_url_key_fallback(self):
        """Builder emits 'url' instead of 'repo' for git-subdir sources."""
        result = _resolve_git_subdir_source({"url": "owner/mono", "path": "pkg", "ref": "v1.0"})
        assert result == "owner/mono/pkg#v1.0"

    def test_repo_key_takes_precedence_over_url(self):
        """When both 'repo' and 'url' are present, 'repo' wins."""
        result = _resolve_git_subdir_source(
            {"repo": "owner/primary", "url": "owner/fallback", "subdir": "pkg"}
        )
        assert result == "owner/primary/pkg"


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

    def test_source_discriminator_key(self):
        """New builder format uses 'source' as discriminator instead of 'type'."""
        p = MarketplacePlugin(
            name="test",
            source={"source": "github", "repo": "owner/repo", "ref": "v1.0"},
        )
        assert resolve_plugin_source(p) == "owner/repo#v1.0"

    def test_source_discriminator_git_subdir(self):
        """New builder format for git-subdir uses 'source' key and 'url' field."""
        p = MarketplacePlugin(
            name="test",
            source={"source": "git-subdir", "url": "owner/mono", "path": "pkg/a", "ref": "main"},
        )
        assert resolve_plugin_source(p) == "owner/mono/pkg/a#main"

    def test_old_format_repository_key(self):
        """Old marketplace format uses 'type' and 'repository' keys."""
        p = MarketplacePlugin(
            name="test",
            source={"type": "github", "repository": "owner/repo", "ref": "v1.0"},
        )
        assert resolve_plugin_source(p) == "owner/repo#v1.0"

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

    def test_dict_kind_key_instead_of_type(self):
        """``kind: github`` (no ``type``) is normalized for resolution."""
        p = MarketplacePlugin(
            name="k",
            source={
                "kind": "github",
                "repo": "acme/mkt",
                "path": "pkg/x",
            },
        )
        assert resolve_plugin_source(p) == "acme/mkt/pkg/x"

    def test_type_field_case_insensitive(self):
        p = MarketplacePlugin(
            name="k",
            source={
                "type": "GitHub",
                "repo": "acme/mkt",
                "path": "pkg/x",
            },
        )
        assert resolve_plugin_source(p) == "acme/mkt/pkg/x"


class TestOldFormatIntegration:
    """Integration tests verifying old-format marketplace entries resolve correctly."""

    def test_old_github_format_full_pipeline(self) -> None:
        """Old format with type/repository/commit resolves via resolve_plugin_source."""
        plugin = MarketplacePlugin(
            name="legacy-plugin",
            source={
                "type": "github",
                "repository": "acme/legacy-tool",
                "ref": "main",
                "commit": "abc123",
            },
        )
        result = resolve_plugin_source(plugin, "org", "marketplace", plugin_root="")
        assert result == "acme/legacy-tool#main"

    def test_old_git_subdir_format_full_pipeline(self) -> None:
        """Old format with type/url/path resolves via resolve_plugin_source."""
        plugin = MarketplacePlugin(
            name="legacy-subdir",
            source={
                "type": "git-subdir",
                "url": "acme/monorepo",
                "path": "tools/helper",
                "ref": "v2.0",
            },
        )
        result = resolve_plugin_source(plugin, "org", "marketplace", plugin_root="")
        assert result == "acme/monorepo/tools/helper#v2.0"

    def test_old_format_url_with_scheme_rejected(self) -> None:
        """A full URL in the url field is rejected by the scheme guard."""
        plugin = MarketplacePlugin(
            name="bad-url",
            source={
                "type": "git-subdir",
                "url": "https://evil.example.com/payload",
                "path": "x",
                "ref": "main",
            },
        )
        with pytest.raises(ValueError, match=r"expected 'owner/repo' but got a URL"):
            resolve_plugin_source(plugin, "org", "marketplace", plugin_root="")


class TestResolveMarketplacePluginGitLabMonorepo:
    """Non-GitHub FQDN + in-marketplace subdirectory → explicit git+path DependencyReference."""

    @pytest.fixture
    def gitlab_marketplace_source(self) -> MarketplaceSource:
        return MarketplaceSource(
            name="apm-reg",
            owner="epm-ease",
            repo="ai-apm-registry",
            host="gitlab.com",
            branch="main",
        )

    @pytest.fixture
    def self_managed_git_fqdn_source(self) -> MarketplaceSource:
        """Host not in GITLAB_HOST — classified *generic* but still GitLab in practice."""
        return MarketplaceSource(
            name="apm-reg",
            owner="epm-ease",
            repo="ai-apm-registry",
            host="git.epam.com",
            branch="main",
        )

    @staticmethod
    def _manifest_with_plugin(plugin: MarketplacePlugin) -> MarketplaceManifest:
        return MarketplaceManifest(
            name="apm-reg",
            plugins=(plugin,),
            plugin_root="",
        )

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_relative_path_sets_virtual_path_not_in_repo_url(
        self, mock_get, mock_fetch, gitlab_marketplace_source
    ):
        plugin = MarketplacePlugin(
            name="optimize-prompt",
            source="registry/optimize-prompt",
        )
        mock_get.return_value = gitlab_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("optimize-prompt", "apm-reg")
        canonical, resolved = result

        assert resolved.name == "optimize-prompt"
        assert result.dependency_reference is not None
        dep = result.dependency_reference
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "epm-ease/ai-apm-registry"
        assert dep.virtual_path == "registry/optimize-prompt"
        assert dep.is_virtual is True
        assert dep.to_canonical() == canonical

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_self_managed_fqdn_not_in_gitlab_env_still_gets_dependency_reference(
        self, mock_get, mock_fetch, self_managed_git_fqdn_source
    ):
        """Regression: host-qualified git-subdir repo must keep the marketplace project root."""
        plugin = MarketplacePlugin(
            name="optimize-prompt",
            source={
                "type": "git-subdir",
                "repo": "git.epam.com/epm-ease/ai-apm-registry",
                "subdir": "registry/optimize-prompt",
            },
        )
        mock_get.return_value = self_managed_git_fqdn_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("optimize-prompt", "apm-reg")
        dep = result.dependency_reference
        assert dep is not None
        assert dep.host == "git.epam.com"
        assert dep.repo_url == "epm-ease/ai-apm-registry"
        assert dep.virtual_path == "registry/optimize-prompt"
        assert result.canonical == "git.epam.com/epm-ease/ai-apm-registry/registry/optimize-prompt"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_unpack_two_tuple_backward_compatible(
        self, mock_get, mock_fetch, gitlab_marketplace_source
    ):
        plugin = MarketplacePlugin(name="p", source="pkg/a")
        mock_get.return_value = gitlab_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        canonical, resolved_plugin = resolve_marketplace_plugin("p", "apm-reg")
        assert "pkg/a" in canonical
        assert resolved_plugin.name == "p"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_github_host_no_dependency_reference(
        self,
        mock_get,
        mock_fetch,
    ):
        gh_source = MarketplaceSource(
            name="mkt",
            owner="acme",
            repo="marketplace",
            host="github.com",
        )
        plugin = MarketplacePlugin(name="p", source="plugins/foo")
        mock_get.return_value = gh_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("p", "mkt")
        assert result.dependency_reference is None
        assert result.canonical == "acme/marketplace/plugins/foo"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_external_git_subdir_on_gitlab_no_monorepo_rule(
        self, mock_get, mock_fetch, gitlab_marketplace_source
    ):
        plugin = MarketplacePlugin(
            name="ext",
            source={
                "type": "git-subdir",
                "repo": "other/external-repo",
                "subdir": "packages/x",
                "ref": "main",
            },
        )
        mock_get.return_value = gitlab_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("ext", "apm-reg")
        assert result.dependency_reference is None
        assert result.canonical == "other/external-repo/packages/x#main"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_in_marketplace_git_subdir_dict(self, mock_get, mock_fetch, gitlab_marketplace_source):
        plugin = MarketplacePlugin(
            name="mono",
            source={
                "type": "git-subdir",
                "repo": "epm-ease/ai-apm-registry",
                "subdir": "registry/pkg",
                "ref": "v1",
            },
        )
        mock_get.return_value = gitlab_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("mono", "apm-reg")
        assert result.dependency_reference is not None
        dep = result.dependency_reference
        assert dep.repo_url == "epm-ease/ai-apm-registry"
        assert dep.virtual_path == "registry/pkg"
        assert dep.reference == "v1"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_in_marketplace_gitlab_dict_type_gets_dependency_reference(
        self, mock_get, mock_fetch, gitlab_marketplace_source
    ):
        """GitLab-native ``type: gitlab`` must emit structured git+path like ``git-subdir``."""
        plugin = MarketplacePlugin(
            name="mono-gitlab-type",
            source={
                "type": "gitlab",
                "repo": "epm-ease/ai-apm-registry",
                "path": "agents/reverse-architect",
                "ref": "main",
            },
        )
        mock_get.return_value = gitlab_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("mono-gitlab-type", "apm-reg")
        assert result.dependency_reference is not None
        dep = result.dependency_reference
        assert dep.repo_url == "epm-ease/ai-apm-registry"
        assert dep.virtual_path == "agents/reverse-architect"
        assert dep.reference == "main"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_url_source_on_gitlab_preserves_host(
        self, mock_get, mock_fetch, gitlab_marketplace_source
    ):
        """#1848: url-type source on GitLab marketplace must preserve host in dep_ref."""
        plugin = MarketplacePlugin(
            name="my-first-skill",
            source={
                "source": "url",
                "url": "https://gitlab.com/my-name/apm/my-first-skill",
                "ref": "v1.0.0",
            },
        )
        mock_get.return_value = gitlab_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("my-first-skill", "apm-reg")
        assert result.dependency_reference is not None
        dep = result.dependency_reference
        assert dep.host == "gitlab.com"
        assert dep.repo_url == "my-name/apm/my-first-skill"
        assert dep.reference == "v1.0.0"
        assert result.canonical == "gitlab.com/my-name/apm/my-first-skill#v1.0.0"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_url_source_on_gitlab_with_version_spec_override(
        self, mock_get, mock_fetch, gitlab_marketplace_source
    ):
        """#1848: version_spec overrides the ref from the url source."""
        plugin = MarketplacePlugin(
            name="my-skill",
            source={
                "source": "url",
                "url": "https://gitlab.com/org/repo",
                "ref": "v1.0.0",
            },
        )
        mock_get.return_value = gitlab_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("my-skill", "apm-reg", version_spec="v2.0.0")
        assert result.dependency_reference is not None
        dep = result.dependency_reference
        assert dep.host == "gitlab.com"
        assert dep.reference == "v2.0.0"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_url_source_on_github_no_dep_ref(self, mock_get, mock_fetch):
        """url-type source on github.com marketplace should NOT build dep_ref (existing behaviour)."""
        github_source = MarketplaceSource(
            name="gh-mkt",
            owner="owner",
            repo="marketplace-repo",
            host="github.com",
            branch="main",
        )
        plugin = MarketplacePlugin(
            name="pkg",
            source={
                "source": "url",
                "url": "https://github.com/other/repo",
            },
        )
        mock_get.return_value = github_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("pkg", "gh-mkt")
        # github.com marketplace does not need explicit git path -> no dep_ref built
        assert result.dependency_reference is None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_external_gitlab_dict_type_no_monorepo_rule(
        self, mock_get, mock_fetch, gitlab_marketplace_source
    ):
        plugin = MarketplacePlugin(
            name="ext-gitlab",
            source={
                "type": "gitlab",
                "repo": "other/external-repo",
                "path": "packages/x",
            },
        )
        mock_get.return_value = gitlab_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("ext-gitlab", "apm-reg")
        assert result.dependency_reference is None
        assert "other/external-repo" in result.canonical

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_repo_match_normalizes_git_suffix_and_case(
        self, mock_get, mock_fetch, gitlab_marketplace_source
    ):
        plugin = MarketplacePlugin(
            name="z",
            source={
                "type": "github",
                "repo": "Epm-Ease/AI-APM-Registry.git",
                "path": "registry/z",
            },
        )
        mock_get.return_value = gitlab_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("z", "apm-reg")
        assert result.dependency_reference is not None
        assert result.dependency_reference.virtual_path == "registry/z"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_path_traversal_still_rejected(self, mock_get, mock_fetch, gitlab_marketplace_source):
        plugin = MarketplacePlugin(name="bad", source="../escape")
        mock_get.return_value = gitlab_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        with pytest.raises(ValueError, match="traversal"):
            resolve_marketplace_plugin("bad", "apm-reg")

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_gitlab_host_env_relative_source_sets_dependency_reference(
        self, mock_get, mock_fetch, self_managed_git_fqdn_source, monkeypatch
    ):
        """With GITLAB_HOST, monorepos still get structured ref (install must not re-parse FQDN)."""
        monkeypatch.setenv("GITLAB_HOST", "git.epam.com")
        plugin = MarketplacePlugin(
            name="reverse-architect",
            source="agents/reverse-architect",
        )
        mock_get.return_value = self_managed_git_fqdn_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("reverse-architect", "apm-reg")
        dep = result.dependency_reference
        assert dep is not None
        assert dep.host == "git.epam.com"
        assert dep.repo_url == "epm-ease/ai-apm-registry"
        assert dep.virtual_path == "agents/reverse-architect"
        assert dep.is_virtual is True
        # Same result as explicit object form (the shape install expects)
        from_dict = DependencyReference.parse_from_dict(
            {
                "git": "https://git.epam.com/epm-ease/ai-apm-registry.git",
                "path": "agents/reverse-architect",
            }
        )
        assert dep.repo_url == from_dict.repo_url
        assert dep.virtual_path == from_dict.virtual_path

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_apm_gitlab_hosts_env_list_sets_dependency_reference(
        self, mock_get, mock_fetch, self_managed_git_fqdn_source, monkeypatch
    ):
        """APM_GITLAB_HOSTS must classify the host the same for parity with GITLAB_HOST."""
        monkeypatch.delenv("GITLAB_HOST", raising=False)
        monkeypatch.setenv("APM_GITLAB_HOSTS", "other.example.com,git.epam.com")
        plugin = MarketplacePlugin(name="p", source="registry/pkg")
        mock_get.return_value = self_managed_git_fqdn_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("p", "apm-reg")
        dep = result.dependency_reference
        assert dep is not None
        assert dep.repo_url == "epm-ease/ai-apm-registry"
        assert dep.virtual_path == "registry/pkg"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_kind_key_github_dict_in_marketplace_gets_structured_ref(
        self, mock_get, mock_fetch, gitlab_marketplace_source
    ):
        """``kind: github`` (Claude) without ``type`` key must still match the marketplace repo."""
        plugin = MarketplacePlugin(
            name="k",
            source={
                "kind": "github",
                "repo": "epm-ease/ai-apm-registry",
                "path": "registry/pkg",
            },
        )
        mock_get.return_value = gitlab_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("k", "apm-reg")
        assert result.dependency_reference is not None
        dep = result.dependency_reference
        assert dep.virtual_path == "registry/pkg"
        assert dep.repo_url == "epm-ease/ai-apm-registry"


class TestResolveMarketplacePluginGHECloud:
    """GHE Cloud (``*.ghe.com``) marketplaces must carry host in canonical (issue #1285).

    GitHub-family hosts keep virtual shorthand (no ``dependency_reference``) because
    they have no GitLab-style nested-group ambiguity. ``github.com`` is the
    ``DependencyReference.parse`` default so a bare ``owner/repo`` canonical
    self-routes; ``*.ghe.com`` is not, so canonical must carry the host forward or
    downstream auth lands on ``github.com`` with the wrong credentials.
    """

    @pytest.fixture
    def ghe_marketplace_source(self) -> MarketplaceSource:
        return MarketplaceSource(
            name="my-marketplace",
            owner="myorg",
            repo="my-marketplace",
            host="corp.ghe.com",
            branch="main",
        )

    @staticmethod
    def _manifest_with_plugin(plugin: MarketplacePlugin) -> MarketplaceManifest:
        return MarketplaceManifest(
            name="my-marketplace",
            plugins=(plugin,),
            plugin_root="",
        )

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_relative_source_carries_host_in_canonical(
        self, mock_get, mock_fetch, ghe_marketplace_source
    ):
        plugin = MarketplacePlugin(name="my-plugin", source="./plugins/my-plugin")
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("my-plugin", "my-marketplace")

        assert result.canonical == "corp.ghe.com/myorg/my-marketplace/plugins/my-plugin"
        # GHE keeps shorthand semantics -- no structured dep_ref, only canonical
        assert result.dependency_reference is None
        # The whole point: parse must recover the GHE host instead of defaulting to github.com
        dep = DependencyReference.parse(result.canonical)
        assert dep.host == "corp.ghe.com"
        assert dep.repo_url == "myorg/my-marketplace"
        assert dep.virtual_path == "plugins/my-plugin"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_dict_github_source_bare_repo_carries_host(
        self, mock_get, mock_fetch, ghe_marketplace_source
    ):
        """Dict ``github`` source whose bare ``repo`` matches the marketplace project."""
        plugin = MarketplacePlugin(
            name="dict-bare",
            source={
                "type": "github",
                "repo": "myorg/my-marketplace",
                "path": "plugins/my-plugin",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("dict-bare", "my-marketplace")
        assert result.canonical == "corp.ghe.com/myorg/my-marketplace/plugins/my-plugin"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_dict_github_source_host_qualified_repo_not_double_prefixed(
        self, mock_get, mock_fetch, ghe_marketplace_source
    ):
        """Idempotent: dict source already carrying the host in ``repo`` keeps a single prefix."""
        plugin = MarketplacePlugin(
            name="dict-qualified",
            source={
                "type": "github",
                "repo": "corp.ghe.com/myorg/my-marketplace",
                "path": "plugins/my-plugin",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("dict-qualified", "my-marketplace")
        assert result.canonical == "corp.ghe.com/myorg/my-marketplace/plugins/my-plugin"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_dict_github_source_mixed_case_host_qualified_not_double_prefixed(
        self, mock_get, mock_fetch, ghe_marketplace_source
    ):
        """Case-insensitive idempotent check: manifests may write host in any case."""
        plugin = MarketplacePlugin(
            name="dict-mixed",
            source={
                "type": "github",
                "repo": "Corp.GHE.com/myorg/my-marketplace",
                "path": "plugins/my-plugin",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("dict-mixed", "my-marketplace")
        # Manifest-supplied case is preserved; no second prefix is added
        assert result.canonical == "Corp.GHE.com/myorg/my-marketplace/plugins/my-plugin"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_cross_repo_source_not_prefixed(self, mock_get, mock_fetch, ghe_marketplace_source):
        """Cross-repo dict source is out of scope; canonical is left to the existing parse default.

        Bare unqualified ``repo`` pointing to a different project carries no signal that
        it lives on the marketplace host; treating it as such would silently change the
        host routing of every cross-repo plugin entry. Manifest authors must
        host-qualify cross-host references explicitly.
        """
        plugin = MarketplacePlugin(
            name="cross-repo",
            source={
                "type": "github",
                "repo": "anotherorg/anothertool",
                "path": "plugins/my-plugin",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("cross-repo", "my-marketplace")
        assert result.canonical == "anotherorg/anothertool/plugins/my-plugin"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_version_spec_override_preserves_host_prefix(
        self, mock_get, mock_fetch, ghe_marketplace_source
    ):
        """``version_spec`` raw-ref override stacks correctly on a host-prefixed canonical."""
        plugin = MarketplacePlugin(name="ref-overridden", source="./plugins/ref-overridden")
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin(
            "ref-overridden", "my-marketplace", version_spec="release-2.0"
        )
        assert (
            result.canonical
            == "corp.ghe.com/myorg/my-marketplace/plugins/ref-overridden#release-2.0"
        )
        dep = DependencyReference.parse(result.canonical)
        assert dep.host == "corp.ghe.com"
        assert dep.reference == "release-2.0"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_url_form_repo_not_prefixed(self, mock_get, mock_fetch, ghe_marketplace_source):
        """Dict source whose ``repo`` is a full ``https://`` URL must not be host-prefixed.

        ``_resolve_github_source`` only validates ``/`` in ``repo`` so a full URL slips
        through and produces a canonical that already carries a scheme + host. Prefixing
        again would yield a malformed ``corp.ghe.com/https://...`` string that
        ``DependencyReference.parse`` rejects with ValueError.
        """
        plugin = MarketplacePlugin(
            name="url-form",
            source={
                "type": "github",
                "repo": "https://corp.ghe.com/myorg/my-marketplace",
                "path": "plugins/url-form",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("url-form", "my-marketplace")
        assert result.canonical == "https://corp.ghe.com/myorg/my-marketplace/plugins/url-form"
        # Downstream parse still recovers the GHE host from the URL form natively.
        dep = DependencyReference.parse(result.canonical)
        assert dep.host == "corp.ghe.com"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_ssh_form_repo_not_prefixed(self, mock_get, mock_fetch, ghe_marketplace_source):
        """Dict source whose ``repo`` is an SSH SCP shorthand must not be host-prefixed.

        Same class as the URL-form regression: ``git@host:owner/repo`` carries its own
        host and an SCP-style ``:`` path separator that breaks a naive ``/`` split.
        """
        plugin = MarketplacePlugin(
            name="ssh-form",
            source={
                "type": "github",
                "repo": "git@corp.ghe.com:myorg/my-marketplace",
                "path": "plugins/ssh-form",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("ssh-form", "my-marketplace")
        assert result.canonical == "git@corp.ghe.com:myorg/my-marketplace/plugins/ssh-form"
        dep = DependencyReference.parse(result.canonical)
        assert dep.host == "corp.ghe.com"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_github_com_canonical_remains_bare(self, mock_get, mock_fetch):
        """Regression: github.com marketplace canonical stays bare (parse default applies)."""
        gh_source = MarketplaceSource(
            name="my-marketplace",
            owner="myorg",
            repo="my-marketplace",
            host="github.com",
            branch="main",
        )
        plugin = MarketplacePlugin(name="my-plugin", source="./plugins/my-plugin")
        mock_get.return_value = gh_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("my-plugin", "my-marketplace")
        assert result.canonical == "myorg/my-marketplace/plugins/my-plugin"
        assert result.dependency_reference is None


class TestCrossRepoMisconfigRisk:
    """``MarketplacePluginResolution.cross_repo_misconfig_risk`` for #1305.

    PR #1292 narrowly scoped its ``*.ghe.com`` host backfill to in-marketplace
    sources because cross-repo dict ``repo`` syntax legitimately serves two
    intents on an enterprise marketplace (open-source ``github.com`` dep vs
    misconfigured same-host entry). The resolver cannot distinguish them, but
    it can flag the signature so the install command surfaces an actionable
    hint when validation later fails. These tests lock that signature shape.
    """

    @pytest.fixture
    def ghe_marketplace_source(self) -> MarketplaceSource:
        return MarketplaceSource(
            name="my-marketplace",
            owner="myorg",
            repo="my-marketplace",
            host="corp.ghe.com",
            branch="main",
        )

    @staticmethod
    def _manifest_with_plugin(plugin: MarketplacePlugin) -> MarketplaceManifest:
        return MarketplaceManifest(
            name="my-marketplace",
            plugins=(plugin,),
            plugin_root="",
        )

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_cross_repo_bare_attaches_risk(self, mock_get, mock_fetch, ghe_marketplace_source):
        """Textbook #1305: ``type: github`` + bare cross-repo ``repo`` on ``*.ghe.com``."""
        plugin = MarketplacePlugin(
            name="shared-tool",
            source={
                "type": "github",
                "repo": "platform-team/shared-tool",
                "path": "plugins/shared",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("shared-tool", "my-marketplace")
        risk = result.cross_repo_misconfig_risk
        assert risk is not None
        assert risk.marketplace_host == "corp.ghe.com"
        assert risk.bare_repo_field == "platform-team/shared-tool"
        assert risk.suggested_qualified_repo == "corp.ghe.com/platform-team/shared-tool"
        # Resolver leaves canonical bare -- behavior contract unchanged
        assert result.canonical == "platform-team/shared-tool/plugins/shared"
        assert result.dependency_reference is None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_cross_repo_inferred_github_via_path_attaches_risk(
        self, mock_get, mock_fetch, ghe_marketplace_source
    ):
        """Dict source with no ``type`` but with ``path`` is inferred ``github``."""
        plugin = MarketplacePlugin(
            name="inferred",
            source={
                "repo": "platform-team/shared-tool",
                "path": "plugins/inferred",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("inferred", "my-marketplace")
        assert result.cross_repo_misconfig_risk is not None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_cross_repo_kind_github_attaches_risk(
        self, mock_get, mock_fetch, ghe_marketplace_source
    ):
        """``kind: github`` (Claude-style) routes through the same path."""
        plugin = MarketplacePlugin(
            name="kind-style",
            source={
                "kind": "github",
                "repo": "platform-team/shared-tool",
                "path": "plugins/kind-style",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("kind-style", "my-marketplace")
        assert result.cross_repo_misconfig_risk is not None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_cross_repo_uppercase_type_attaches_risk(
        self, mock_get, mock_fetch, ghe_marketplace_source
    ):
        """``type: GitHub`` (mixed case) is normalized to ``github``."""
        plugin = MarketplacePlugin(
            name="upper",
            source={
                "type": "GitHub",
                "repo": "platform-team/shared-tool",
                "path": "plugins/upper",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("upper", "my-marketplace")
        assert result.cross_repo_misconfig_risk is not None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_cross_repo_host_qualified_no_risk(self, mock_get, mock_fetch, ghe_marketplace_source):
        """``repo: corp.ghe.com/owner/repo`` already routes; no hint needed."""
        plugin = MarketplacePlugin(
            name="qualified",
            source={
                "type": "github",
                "repo": "corp.ghe.com/platform-team/shared-tool",
                "path": "plugins/qualified",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("qualified", "my-marketplace")
        assert result.cross_repo_misconfig_risk is None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_cross_repo_qualified_to_github_com_no_risk(
        self, mock_get, mock_fetch, ghe_marketplace_source
    ):
        """#1326 cross-host explicit qualification: ``repo: github.com/owner/repo``
        on a ``*.ghe.com`` marketplace is declared cross-host intent, NOT a
        dependency-confusion ambiguity. The sentinel must not attach
        (otherwise the install gate would refuse a legitimate cross-host
        dependency the operator explicitly declared).

        The same-host idempotency path in ``_needs_canonical_host_prefix``
        only handles ``repo: corp.ghe.com/owner/repo``; this case is the
        symmetric escape hatch for cross-host intent at the resolver layer.
        """
        plugin = MarketplacePlugin(
            name="cross-host",
            source={
                "type": "github",
                "repo": "github.com/platform-team/shared-tool",
                "path": "plugins/cross-host",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("cross-host", "my-marketplace")
        assert result.cross_repo_misconfig_risk is None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_cross_repo_url_form_no_risk(self, mock_get, mock_fetch, ghe_marketplace_source):
        """Full ``https://`` URL carries its own host; hint inapplicable."""
        plugin = MarketplacePlugin(
            name="url",
            source={
                "type": "github",
                "repo": "https://corp.ghe.com/platform-team/shared-tool",
                "path": "plugins/url",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("url", "my-marketplace")
        assert result.cross_repo_misconfig_risk is None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_cross_repo_ssh_form_no_risk(self, mock_get, mock_fetch, ghe_marketplace_source):
        """SSH SCP shorthand carries its own host; hint inapplicable."""
        plugin = MarketplacePlugin(
            name="ssh",
            source={
                "type": "github",
                "repo": "git@corp.ghe.com:platform-team/shared-tool",
                "path": "plugins/ssh",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("ssh", "my-marketplace")
        assert result.cross_repo_misconfig_risk is None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_cross_repo_gitlab_type_no_risk(self, mock_get, mock_fetch, ghe_marketplace_source):
        """``type: gitlab`` cross-repo hits the same routing bug but the
        "host-qualify with marketplace host" remediation does not match the
        operator's intent (they meant gitlab.com, not corp.ghe.com)."""
        plugin = MarketplacePlugin(
            name="gl",
            source={
                "type": "gitlab",
                "repo": "platform-team/shared-tool",
                "path": "plugins/gl",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("gl", "my-marketplace")
        assert result.cross_repo_misconfig_risk is None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_cross_repo_git_subdir_type_no_risk(self, mock_get, mock_fetch, ghe_marketplace_source):
        """``type: git-subdir`` cross-repo: same wording mismatch as gitlab."""
        plugin = MarketplacePlugin(
            name="gs",
            source={
                "type": "git-subdir",
                "repo": "platform-team/shared-tool",
                "subdir": "plugins/gs",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("gs", "my-marketplace")
        assert result.cross_repo_misconfig_risk is None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_in_marketplace_dict_source_no_risk(self, mock_get, mock_fetch, ghe_marketplace_source):
        """In-marketplace dict source (PR #1292's domain) does not get a risk flag."""
        plugin = MarketplacePlugin(
            name="in-mkt",
            source={
                "type": "github",
                "repo": "myorg/my-marketplace",
                "path": "plugins/in-mkt",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("in-mkt", "my-marketplace")
        assert result.cross_repo_misconfig_risk is None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_in_marketplace_string_source_no_risk(
        self, mock_get, mock_fetch, ghe_marketplace_source
    ):
        """Relative string source is always in-marketplace; no risk flag."""
        plugin = MarketplacePlugin(name="rel", source="./plugins/rel")
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("rel", "my-marketplace")
        assert result.cross_repo_misconfig_risk is None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_github_com_marketplace_cross_repo_no_risk(self, mock_get, mock_fetch):
        """Cross-repo on a plain ``github.com`` marketplace: bare canonical
        is correct (parse default matches the marketplace host) so no hint."""
        gh_source = MarketplaceSource(
            name="my-marketplace",
            owner="myorg",
            repo="my-marketplace",
            host="github.com",
            branch="main",
        )
        plugin = MarketplacePlugin(
            name="cross",
            source={
                "type": "github",
                "repo": "platform-team/shared-tool",
                "path": "plugins/cross",
            },
        )
        mock_get.return_value = gh_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("cross", "my-marketplace")
        assert result.cross_repo_misconfig_risk is None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_cross_repo_source_field_synonym_attaches_risk(
        self, mock_get, mock_fetch, ghe_marketplace_source
    ):
        """``source: github`` synonym (third leg of the ``type``/``kind``/``source`` trio)."""
        plugin = MarketplacePlugin(
            name="src-key",
            source={
                "source": "github",
                "repo": "platform-team/shared-tool",
                "path": "plugins/src-key",
            },
        )
        mock_get.return_value = ghe_marketplace_source
        mock_fetch.return_value = self._manifest_with_plugin(plugin)

        result = resolve_marketplace_plugin("src-key", "my-marketplace")
        assert result.cross_repo_misconfig_risk is not None

    def test_compute_returns_none_on_url_or_scp_repo_field_when_filter_bypassed(
        self,
    ):
        """Defense-in-depth: ``_needs_canonical_host_prefix`` already returns
        False for URL / SCP shorthand canonicals (its ``":"`` in first-segment
        clause), so these forms normally short-circuit before reaching the
        explicit-host guard. This direct-call test simulates a future upstream
        refactor that lets those forms through and asserts the guard still
        recognises them as host-qualified -- a bare ``split("/", 1)[0]`` would
        misclassify ``https:`` / ``git@host:owner`` as non-host first segments
        and incorrectly attach the sentinel.

        Calls ``_compute_cross_repo_misconfig_risk`` directly with a
        canonical that bypasses the upstream guard so we can lock the
        behaviour of the explicit-host extraction step alone.
        """
        from apm_cli.marketplace.resolver import _compute_cross_repo_misconfig_risk

        source = MarketplaceSource(
            name="my-marketplace",
            owner="myorg",
            repo="my-marketplace",
            host="corp.ghe.com",
            branch="main",
        )

        for repo_value in (
            "https://github.com/platform-team/shared-tool",
            "http://github.com/platform-team/shared-tool",
            "ssh://github.com/platform-team/shared-tool",
            "git@github.com:platform-team/shared-tool",
        ):
            plugin = MarketplacePlugin(
                name="cross",
                source={
                    "type": "github",
                    "repo": repo_value,
                    "path": "plugins/cross",
                },
            )
            # Hand-build a canonical that would bypass the upstream
            # ``_needs_canonical_host_prefix`` URL/SCP short-circuit (this
            # shape is not what ``_resolve_github_source`` actually produces
            # for these inputs; the test is intentionally probing the
            # explicit-host guard in isolation).
            canonical = "platform-team/shared-tool/plugins/cross"
            risk = _compute_cross_repo_misconfig_risk(plugin, source, canonical, None)
            assert risk is None, (
                f"explicit-host guard must recognise {repo_value!r} as "
                "host-qualified even when upstream filters do not catch it"
            )

    def test_compute_returns_none_on_no_slash_repo_field(self):
        """Defensive guard inside the helper: ``repo`` without ``/`` is
        rejected by ``_resolve_github_source`` upstream, but if a future
        refactor ever bypasses that we must not synthesize a nonsense
        ``host/no-slash`` suggestion. Calls the helper directly because
        no real resolver flow lets us reach it with this input."""
        from apm_cli.marketplace.resolver import _compute_cross_repo_misconfig_risk

        plugin = MarketplacePlugin(
            name="bad",
            source={
                "type": "github",
                "repo": "no-slash",
                "path": "plugins/bad",
            },
        )
        source = MarketplaceSource(
            name="my-marketplace",
            owner="myorg",
            repo="my-marketplace",
            host="corp.ghe.com",
            branch="main",
        )
        # Hand-build a plausible (if malformed) canonical the way
        # ``_resolve_github_source`` would have if its guard were removed.
        canonical = "no-slash/plugins/bad"
        risk = _compute_cross_repo_misconfig_risk(plugin, source, canonical, None)
        assert risk is None


class TestGitLabShorthandParseVsStructuredRef:
    """``DependencyReference.parse`` on a long FQDN does not split monorepo paths on GitLab hosts."""

    def test_fqdn_shorthand_without_git_path_misclassifies(self, monkeypatch):
        # Install must use structured object-form; plain shorthand is not safe to re-parse.
        monkeypatch.setenv("GITLAB_HOST", "git.epam.com")
        bad = DependencyReference.parse(
            "git.epam.com/epm-ease/ai-apm-registry/agents/reverse-architect"
        )
        assert bad.is_virtual is False
        assert "agents" in bad.repo_url
        good = DependencyReference.parse_from_dict(
            {
                "git": "https://git.epam.com/epm-ease/ai-apm-registry.git",
                "path": "agents/reverse-architect",
            }
        )
        assert good.is_virtual is True
        assert good.repo_url == "epm-ease/ai-apm-registry"
        assert good.virtual_path == "agents/reverse-architect"


class TestMarketplaceRegisteredRefPropagation:
    """Registered marketplace ``--ref`` must propagate to plugin canonicals.

    When a marketplace is added with ``apm marketplace add --ref feat/x owner/repo``,
    subsequent ``apm install plugin@marketplace`` must download the plugin from
    ``feat/x`` -- not from the repo's default branch.  This applies to both:

    - GitHub-family marketplaces (no ``dependency_reference``; ref embedded in canonical)
    - GitLab / non-GitHub marketplaces (ref embedded in ``dependency_reference``)
    """

    @staticmethod
    def _github_source(ref: str = "main") -> MarketplaceSource:
        return MarketplaceSource(
            name="my-marketplace",
            owner="acme",
            repo="plugins",
            host="github.com",
            ref=ref,
        )

    @staticmethod
    def _gitlab_source(ref: str = "main") -> MarketplaceSource:
        return MarketplaceSource(
            name="my-marketplace",
            owner="acme",
            repo="plugins",
            host="gitlab.com",
            ref=ref,
        )

    @staticmethod
    def _manifest(plugin: MarketplacePlugin) -> MarketplaceManifest:
        return MarketplaceManifest(name="my-marketplace", plugins=(plugin,), plugin_root="")

    # -- GitHub-family: ref embedded in canonical ---------------------------------

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_github_string_source_registered_ref_appended(self, mock_get, mock_fetch):
        """Non-main marketplace ref must appear as ``#ref`` suffix in the canonical."""
        plugin = MarketplacePlugin(name="my-plugin", source="./plugins/my-plugin")
        mock_get.return_value = self._github_source(ref="feat/my-feature")
        mock_fetch.return_value = self._manifest(plugin)

        result = resolve_marketplace_plugin("my-plugin", "my-marketplace")

        assert result.canonical == "acme/plugins/plugins/my-plugin#feat/my-feature"
        assert result.dependency_reference is None

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_github_string_source_main_ref_not_appended(self, mock_get, mock_fetch):
        """Default ``main`` ref must NOT be embedded (implicit; avoids cluttering canonicals)."""
        plugin = MarketplacePlugin(name="my-plugin", source="./plugins/my-plugin")
        mock_get.return_value = self._github_source(ref="main")
        mock_fetch.return_value = self._manifest(plugin)

        result = resolve_marketplace_plugin("my-plugin", "my-marketplace")

        assert "#" not in result.canonical

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_github_string_source_head_ref_not_appended(self, mock_get, mock_fetch):
        """``HEAD`` ref, like ``main``, must not be embedded."""
        plugin = MarketplacePlugin(name="my-plugin", source="./plugins/my-plugin")
        mock_get.return_value = self._github_source(ref="HEAD")
        mock_fetch.return_value = self._manifest(plugin)

        result = resolve_marketplace_plugin("my-plugin", "my-marketplace")

        assert "#" not in result.canonical

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_github_string_source_version_spec_overrides_registered_ref(
        self, mock_get, mock_fetch
    ):
        """Explicit ``version_spec`` (``apm install plugin@mkt#v2``) must win over source.ref."""
        plugin = MarketplacePlugin(name="my-plugin", source="./plugins/my-plugin")
        mock_get.return_value = self._github_source(ref="feat/my-feature")
        mock_fetch.return_value = self._manifest(plugin)

        result = resolve_marketplace_plugin(
            "my-plugin", "my-marketplace", version_spec="v2.0.0"
        )

        assert result.canonical.endswith("#v2.0.0")
        assert "feat/my-feature" not in result.canonical

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_github_dict_source_registered_ref_not_propagated(self, mock_get, mock_fetch):
        """Dict sources with an explicit ``ref`` must not be overwritten by source.ref."""
        plugin = MarketplacePlugin(
            name="dict-plugin",
            source={"type": "github", "repo": "acme/plugins", "path": "p", "ref": "v1.0.0"},
        )
        mock_get.return_value = self._github_source(ref="feat/my-feature")
        mock_fetch.return_value = self._manifest(plugin)

        result = resolve_marketplace_plugin("dict-plugin", "my-marketplace")

        assert result.canonical.endswith("#v1.0.0"), (
            "Explicit plugin ref must survive; marketplace ref must not overwrite it"
        )

    # -- GitLab: ref embedded in dependency_reference ----------------------------

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_gitlab_string_source_registered_ref_propagated(self, mock_get, mock_fetch):
        """GitLab monorepo: marketplace ref must reach dep_ref when path_ref is None."""
        plugin = MarketplacePlugin(name="my-plugin", source="registry/my-plugin")
        mock_get.return_value = self._gitlab_source(ref="feat/my-feature")
        mock_fetch.return_value = self._manifest(plugin)

        result = resolve_marketplace_plugin("my-plugin", "my-marketplace")

        assert result.dependency_reference is not None
        dep = result.dependency_reference
        assert dep.reference == "feat/my-feature", (
            "Registered marketplace ref must be threaded into the DependencyReference"
        )
        assert dep.virtual_path == "registry/my-plugin"

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_gitlab_string_source_main_ref_not_propagated(self, mock_get, mock_fetch):
        """Default ``main`` ref on a GitLab marketplace must not produce a dep_ref ref field."""
        plugin = MarketplacePlugin(name="my-plugin", source="registry/my-plugin")
        mock_get.return_value = self._gitlab_source(ref="main")
        mock_fetch.return_value = self._manifest(plugin)

        result = resolve_marketplace_plugin("my-plugin", "my-marketplace")

        dep = result.dependency_reference
        assert dep is not None
        assert dep.reference is None, (
            "Default 'main' ref must not be embedded in DependencyReference"
        )
