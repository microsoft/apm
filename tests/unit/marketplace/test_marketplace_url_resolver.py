"""Tests for URL-source resolver behaviour.

Covers:
- resolve_plugin_source() with skill-md and archive Agent Skills types
- _resolve_url_source() with non-GitHub HTTPS URLs
- resolve_marketplace_plugin() end-to-end with a URL marketplace source
"""

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.marketplace.errors import PluginNotFoundError
from apm_cli.marketplace.models import MarketplaceManifest, MarketplacePlugin, MarketplaceSource
from apm_cli.marketplace.resolver import (
    _resolve_url_source,
    resolve_marketplace_plugin,
    resolve_plugin_source,
)

_VALID_DIGEST = "sha256:" + "a" * 64
_SKILL_URL = "https://example.com/.well-known/agent-skills/code-review/SKILL.md"
_ARCHIVE_URL = "https://example.com/.well-known/agent-skills/my-toolset.tar.gz"


@pytest.fixture
def skill_md_plugin():
    return MarketplacePlugin(
        name="code-review",
        description="Code review helper",
        source={"type": "skill-md", "url": _SKILL_URL, "digest": _VALID_DIGEST},
        source_marketplace="example-skills",
    )


@pytest.fixture
def archive_plugin():
    return MarketplacePlugin(
        name="my-toolset",
        description="A set of tools",
        source={"type": "archive", "url": _ARCHIVE_URL, "digest": _VALID_DIGEST},
        source_marketplace="example-skills",
    )


@pytest.fixture
def url_source():
    return MarketplaceSource(
        name="example-skills",
        source_type="url",
        url="https://example.com/.well-known/agent-skills/index.json",
    )


# ---------------------------------------------------------------------------
# resolve_plugin_source -- skill-md and archive types (t8-test-02, t8-test-03)
# ---------------------------------------------------------------------------


class TestResolvePluginSourceAgentSkills:
    """skill-md and archive source types must resolve to their download URL."""

    def test_skill_md_returns_url(self, skill_md_plugin):
        result = resolve_plugin_source(skill_md_plugin)
        assert result == _SKILL_URL

    def test_archive_returns_url(self, archive_plugin):
        result = resolve_plugin_source(archive_plugin)
        assert result == _ARCHIVE_URL

    def test_skill_md_missing_url_raises(self):
        """skill-md with no url field must raise ValueError, not return empty string."""
        plugin = MarketplacePlugin(
            name="broken",
            source={"type": "skill-md", "digest": _VALID_DIGEST},
        )
        with pytest.raises(ValueError, match="url"):
            resolve_plugin_source(plugin)

    def test_archive_missing_url_raises(self):
        plugin = MarketplacePlugin(
            name="broken",
            source={"type": "archive", "digest": _VALID_DIGEST},
        )
        with pytest.raises(ValueError, match="url"):
            resolve_plugin_source(plugin)


# ---------------------------------------------------------------------------
# _resolve_url_source -- non-GitHub HTTPS allowed (t8-test-04, t8-test-05)
# ---------------------------------------------------------------------------


class TestResolveUrlSource:
    """_resolve_url_source() must pass non-GitHub HTTPS URLs through unchanged."""

    def test_non_github_https_returns_url_directly(self):
        source = {"type": "url", "url": "https://cdn.example.com/plugin"}
        assert _resolve_url_source(source) == "https://cdn.example.com/plugin"

    def test_cdn_url_with_path_returns_unchanged(self):
        url = "https://cdn.example.com/skills/v1/my-plugin"
        assert _resolve_url_source({"url": url}) == url

    def test_github_com_url_still_extracts_owner_repo(self):
        """GitHub.com URLs must keep the existing owner/repo extraction (regression)."""
        source = {"url": "https://github.com/owner/plugin-repo"}
        assert _resolve_url_source(source) == "owner/plugin-repo"

    def test_github_com_url_with_git_suffix(self):
        source = {"url": "https://github.com/owner/plugin-repo.git"}
        assert _resolve_url_source(source) == "owner/plugin-repo"

    def test_empty_url_raises(self):
        with pytest.raises(ValueError):
            _resolve_url_source({"url": ""})

    @pytest.mark.parametrize("url", [
        "http://example.com/index.json",
        "ftp://example.com/index.json",
        "file:///etc/passwd",
    ])
    def test_non_https_url_raises(self, url):
        with pytest.raises(ValueError, match="HTTPS"):
            _resolve_url_source({"url": url})

    @pytest.mark.parametrize("url", [
        "HTTPS://cdn.example.com/skills",
        "Https://CDN.Example.Com/index.json",
    ])
    def test_mixed_case_https_scheme_accepted(self, url):
        """RFC 3986: scheme is case-insensitive; HTTPS:// must be accepted."""
        assert _resolve_url_source({"url": url}) == url


# ---------------------------------------------------------------------------
# resolve_marketplace_plugin -- URL marketplace end-to-end (t8-test-06)
# ---------------------------------------------------------------------------


class TestResolveMarketplacePluginURL:
    """resolve_marketplace_plugin() must work for URL-based marketplaces."""

    def test_url_marketplace_skill_md_resolves(self, url_source, skill_md_plugin):
        manifest = MarketplaceManifest(
            name="example-skills",
            plugins=(skill_md_plugin,),
        )
        with (
            patch(
                "apm_cli.marketplace.resolver.get_marketplace_by_name",
                return_value=url_source,
            ),
            patch(
                "apm_cli.marketplace.resolver.fetch_or_cache",
                return_value=manifest,
            ),
        ):
            canonical, plugin = resolve_marketplace_plugin(
                "code-review", "example-skills"
            )
        assert canonical == _SKILL_URL
        assert plugin.name == "code-review"

    def test_url_marketplace_passes_empty_owner_repo(self, url_source, skill_md_plugin):
        """URL sources have owner='' and repo='' -- resolver must not crash on this."""
        manifest = MarketplaceManifest(
            name="example-skills",
            plugins=(skill_md_plugin,),
        )
        with (
            patch(
                "apm_cli.marketplace.resolver.get_marketplace_by_name",
                return_value=url_source,
            ),
            patch(
                "apm_cli.marketplace.resolver.fetch_or_cache",
                return_value=manifest,
            ),
        ):
            # Must not raise even though source.owner == "" and source.repo == ""
            canonical, _ = resolve_marketplace_plugin(
                "code-review", "example-skills"
            )
        assert canonical  # some non-empty result returned

    def test_plugin_not_found_raises(self, url_source, skill_md_plugin):
        """Looking up a non-existent plugin raises PluginNotFoundError."""
        manifest = MarketplaceManifest(
            name="example-skills",
            plugins=(skill_md_plugin,),
        )
        with (
            patch(
                "apm_cli.marketplace.resolver.get_marketplace_by_name",
                return_value=url_source,
            ),
            patch(
                "apm_cli.marketplace.resolver.fetch_or_cache",
                return_value=manifest,
            ),
        ):
            with pytest.raises(PluginNotFoundError):
                resolve_marketplace_plugin("nonexistent", "example-skills")
