"""Tests for marketplace resolution.

Covers:
- parse_marketplace_ref with #spec suffix (now treated as raw git ref)
- resolve_marketplace_plugin basic resolution
- resolve_marketplace_plugin backward compat (no version_spec)
- ref immutability check (advisory warning on ref change)
- shadow detection warning routing
- warning_handler callback
"""

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.marketplace.resolver import (
    parse_marketplace_ref,
    resolve_marketplace_plugin,
    resolve_plugin_source,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_plugin(
    name="my-plugin",
    repo="acme-org/my-plugin",
    source_ref="main",
):
    """Build a MarketplacePlugin with a github source."""
    source = {"type": "github", "repo": repo, "ref": source_ref}
    return MarketplacePlugin(
        name=name,
        source=source,
        source_marketplace="test-mkt",
    )


def _make_manifest(plugin):
    return MarketplaceManifest(
        name="test-mkt",
        plugins=(plugin,),
        plugin_root="",
    )


def _make_source():
    return MarketplaceSource(
        name="test-mkt",
        owner="acme-org",
        repo="marketplace",
    )


# ---------------------------------------------------------------------------
# parse_marketplace_ref
# ---------------------------------------------------------------------------


class TestParseMarketplaceRef:
    """Parsing NAME@MARKETPLACE#spec."""

    def test_caret_specifier_rejected(self):
        with pytest.raises(ValueError, match="Semver ranges"):
            parse_marketplace_ref("plugin@mkt#^2.0.0")

    def test_tilde_specifier_rejected(self):
        with pytest.raises(ValueError, match="Semver ranges"):
            parse_marketplace_ref("plugin@mkt#~1.1.0")

    def test_exact_version(self):
        result = parse_marketplace_ref("plugin@mkt#2.1.0")
        assert result == ("plugin", "mkt", "2.1.0")

    def test_range_specifier_rejected(self):
        with pytest.raises(ValueError, match="Semver ranges"):
            parse_marketplace_ref("plugin@mkt#>=1.0.0,<3.0.0")

    def test_raw_git_ref(self):
        result = parse_marketplace_ref("plugin@mkt#main")
        assert result == ("plugin", "mkt", "main")

    def test_no_specifier(self):
        result = parse_marketplace_ref("plugin@mkt")
        assert result == ("plugin", "mkt", None)

    def test_empty_after_hash(self):
        """Trailing # with nothing after is not a valid specifier."""
        result = parse_marketplace_ref("plugin@mkt#")
        assert result is None

    def test_whitespace_preserved_in_spec(self):
        """Outer whitespace is stripped; inner spec is preserved."""
        with pytest.raises(ValueError, match="Semver ranges"):
            parse_marketplace_ref("  plugin@mkt#^2.0.0  ")


# ---------------------------------------------------------------------------
# resolve_marketplace_plugin -- basic resolution
# ---------------------------------------------------------------------------


class TestResolveMarketplacePlugin:
    """Basic resolution without version_spec."""

    def _resolve(self, plugin, version_spec=None, **kwargs):
        manifest = _make_manifest(plugin)
        source = _make_source()

        with (
            patch(
                "apm_cli.marketplace.resolver.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.marketplace.resolver.fetch_or_cache",
                return_value=manifest,
            ),
        ):
            return resolve_marketplace_plugin(
                plugin.name,
                "test-mkt",
                version_spec=version_spec,
                **kwargs,
            )

    def test_no_spec_uses_source_ref(self):
        """Without version_spec, canonical uses the source.ref."""
        plugin = _make_plugin()
        canonical, resolved = self._resolve(plugin)
        assert canonical == "acme-org/my-plugin#main"
        assert resolved.name == "my-plugin"

    def test_raw_ref_overrides_source(self):
        """version_spec is treated as raw git ref override."""
        plugin = _make_plugin()
        canonical, resolved = self._resolve(plugin, version_spec="develop")  # noqa: RUF059
        assert canonical == "acme-org/my-plugin#develop"

    def test_ref_tag_override(self):
        """A tag-like version_spec overrides the source ref."""
        plugin = _make_plugin()
        canonical, _ = self._resolve(plugin, version_spec="v2.0.0")
        assert canonical == "acme-org/my-plugin#v2.0.0"

    def test_plugin_not_found_raises(self):
        """PluginNotFoundError when plugin is not in manifest."""
        from apm_cli.marketplace.errors import PluginNotFoundError

        plugin = _make_plugin(name="existing")
        manifest = _make_manifest(plugin)
        source = _make_source()

        with (
            patch(
                "apm_cli.marketplace.resolver.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.marketplace.resolver.fetch_or_cache",
                return_value=manifest,
            ),
            pytest.raises(PluginNotFoundError),
        ):
            resolve_marketplace_plugin("nonexistent", "test-mkt")


# ---------------------------------------------------------------------------
# Canonical string correctness
# ---------------------------------------------------------------------------


class TestCanonicalString:
    """Verify canonical string format from resolve_plugin_source."""

    def test_github_with_ref(self):
        plugin = _make_plugin(repo="acme-org/my-plugin", source_ref="main")
        canonical = resolve_plugin_source(
            plugin,
            marketplace_owner="acme-org",
            marketplace_repo="marketplace",
            plugin_root="",
        )
        assert canonical == "acme-org/my-plugin#main"

    def test_plugin_root_applied(self):
        plugin = _make_plugin(name="reviewer")  # noqa: F841
        plugin_with_subdir = MarketplacePlugin(
            name="reviewer",
            source={
                "type": "git-subdir",
                "repo": "acme-org/mono",
                "ref": "main",
                "subdir": "plugins/reviewer",
            },
        )
        canonical = resolve_plugin_source(
            plugin_with_subdir,
            marketplace_owner="acme-org",
            marketplace_repo="marketplace",
            plugin_root="",
        )
        assert "acme-org/mono" in canonical


# ---------------------------------------------------------------------------
# Warning handler
# ---------------------------------------------------------------------------


class TestWarningHandler:
    """Verify resolve_marketplace_plugin routes security warnings to handler."""

    def test_immutability_warning_via_handler(self):
        """Ref-swap warning goes through warning_handler, not stdlib."""
        plugin = _make_plugin()
        manifest = _make_manifest(plugin)
        source = _make_source()

        captured = []

        with (
            patch(
                "apm_cli.marketplace.resolver.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.marketplace.resolver.fetch_or_cache",
                return_value=manifest,
            ),
            patch(
                "apm_cli.marketplace.version_pins.check_ref_pin",
                return_value="old-ref-abc",  # pretend ref changed
            ),
            patch(
                "apm_cli.marketplace.version_pins.record_ref_pin",
            ),
        ):
            resolve_marketplace_plugin(
                "my-plugin",
                "test-mkt",
                warning_handler=captured.append,
            )

        # Exactly one immutability warning
        assert len(captured) == 1
        assert "ref changed" in captured[0]
        assert "ref swap attack" in captured[0]
        assert "my-plugin" in captured[0]

    def test_shadow_warning_via_handler(self):
        """Shadow detection warning goes through warning_handler."""
        plugin = _make_plugin()
        manifest = _make_manifest(plugin)
        source = _make_source()

        captured = []

        shadow = MagicMock()
        shadow.marketplace_name = "evil-mkt"

        with (
            patch(
                "apm_cli.marketplace.resolver.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.marketplace.resolver.fetch_or_cache",
                return_value=manifest,
            ),
            patch(
                "apm_cli.marketplace.shadow_detector.detect_shadows",
                return_value=[shadow],
            ),
            patch(
                "apm_cli.marketplace.version_pins.check_ref_pin",
                return_value=None,
            ),
            patch(
                "apm_cli.marketplace.version_pins.record_ref_pin",
            ),
        ):
            resolve_marketplace_plugin(
                "my-plugin",
                "test-mkt",
                warning_handler=captured.append,
            )

        assert len(captured) == 1
        assert "evil-mkt" in captured[0]
        assert "my-plugin" in captured[0]

    def test_no_handler_falls_back_to_stdlib(self, caplog):
        """Without warning_handler, warnings go through Python logging."""
        import logging

        plugin = _make_plugin()
        manifest = _make_manifest(plugin)
        source = _make_source()

        with (
            patch(
                "apm_cli.marketplace.resolver.get_marketplace_by_name",
                return_value=source,
            ),
            patch(
                "apm_cli.marketplace.resolver.fetch_or_cache",
                return_value=manifest,
            ),
            patch(
                "apm_cli.marketplace.version_pins.check_ref_pin",
                return_value="old-ref",
            ),
            patch(
                "apm_cli.marketplace.version_pins.record_ref_pin",
            ),
            caplog.at_level(logging.WARNING, logger="apm_cli.marketplace.resolver"),
        ):
            resolve_marketplace_plugin(
                "my-plugin",
                "test-mkt",
                # No warning_handler -- should use stdlib logging
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1
        assert "ref changed" in warnings[0].message
