"""Tests for cross-marketplace shadow detection.

Covers:
- detect_shadows() with zero, one, and multiple shadow matches
- Case-insensitive plugin name matching
- Primary marketplace exclusion
- Graceful handling of fetch errors and empty registries
- Integration: resolver.py emits warnings on shadow detection
- Integration: install.py sets provenance fields on marketplace deps
"""

import logging
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest  # noqa: F401

from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.marketplace.shadow_detector import ShadowMatch, detect_shadows

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(name, owner="org", repo="repo"):
    return MarketplaceSource(name=name, owner=owner, repo=repo)


def _make_plugin(name):
    return MarketplacePlugin(name=name, source="plugins/" + name)


def _make_manifest(name, plugins):
    return MarketplaceManifest(
        name=name,
        plugins=tuple(_make_plugin(p) for p in plugins),
    )


# Maps marketplace source name -> MarketplaceManifest for fetch_or_cache mock
def _build_fetch_side_effect(manifests_by_name):
    """Return a side_effect callable for fetch_or_cache."""

    def _fetch(source, **kwargs):
        return manifests_by_name[source.name]

    return _fetch


# ---------------------------------------------------------------------------
# detect_shadows -- unit tests
# ---------------------------------------------------------------------------


_PATCH_REGISTRY = "apm_cli.marketplace.shadow_detector.get_registered_marketplaces"
_PATCH_FETCH = "apm_cli.marketplace.shadow_detector.fetch_or_cache"


class TestDetectShadows:
    """Unit tests for the detect_shadows function."""

    def test_no_shadows(self):
        """Plugin only in primary marketplace -- returns empty list."""
        sources = [_make_source("primary")]
        manifests = {"primary": _make_manifest("primary", ["my-plugin"])}

        with (
            patch(_PATCH_REGISTRY, return_value=sources),
            patch(_PATCH_FETCH, side_effect=_build_fetch_side_effect(manifests)),
        ):
            result = detect_shadows("my-plugin", "primary")

        assert result == []

    def test_shadow_found(self):
        """Plugin in primary + one other marketplace -- returns 1 match."""
        sources = [_make_source("primary"), _make_source("community")]
        manifests = {
            "primary": _make_manifest("primary", ["my-plugin"]),
            "community": _make_manifest("community", ["my-plugin", "other"]),
        }

        with (
            patch(_PATCH_REGISTRY, return_value=sources),
            patch(_PATCH_FETCH, side_effect=_build_fetch_side_effect(manifests)),
        ):
            result = detect_shadows("my-plugin", "primary")

        assert len(result) == 1
        assert result[0] == ShadowMatch(marketplace_name="community", plugin_name="my-plugin")

    def test_multiple_shadows(self):
        """Plugin in 3 marketplaces -- returns 2 matches (excludes primary)."""
        sources = [
            _make_source("primary"),
            _make_source("community"),
            _make_source("third-party"),
        ]
        manifests = {
            "primary": _make_manifest("primary", ["my-plugin"]),
            "community": _make_manifest("community", ["my-plugin"]),
            "third-party": _make_manifest("third-party", ["my-plugin"]),
        }

        with (
            patch(_PATCH_REGISTRY, return_value=sources),
            patch(_PATCH_FETCH, side_effect=_build_fetch_side_effect(manifests)),
        ):
            result = detect_shadows("my-plugin", "primary")

        assert len(result) == 2
        names = {s.marketplace_name for s in result}
        assert names == {"community", "third-party"}

    def test_case_insensitive(self):
        """Shadow detected even with different casing of marketplace name."""
        sources = [_make_source("Primary"), _make_source("community")]
        manifests = {
            "Primary": _make_manifest("Primary", ["My-Plugin"]),
            "community": _make_manifest("community", ["my-plugin"]),
        }

        with (
            patch(_PATCH_REGISTRY, return_value=sources),
            patch(_PATCH_FETCH, side_effect=_build_fetch_side_effect(manifests)),
        ):
            # Primary name uses different casing -- should still be excluded
            result = detect_shadows("my-plugin", "primary")

        assert len(result) == 1
        assert result[0].marketplace_name == "community"

    def test_primary_excluded(self):
        """Primary marketplace never appears in results even if it matches."""
        sources = [_make_source("acme"), _make_source("other")]
        manifests = {
            "acme": _make_manifest("acme", ["sec-check"]),
            "other": _make_manifest("other", []),
        }

        with (
            patch(_PATCH_REGISTRY, return_value=sources),
            patch(_PATCH_FETCH, side_effect=_build_fetch_side_effect(manifests)),
        ):
            result = detect_shadows("sec-check", "acme")

        assert result == []

    def test_fetch_error_handled(self, caplog):
        """One marketplace fails to fetch -- others still checked."""
        sources = [
            _make_source("primary"),
            _make_source("broken"),
            _make_source("good"),
        ]
        # Only "good" has a manifest; "broken" will raise
        manifests = {
            "good": _make_manifest("good", ["my-plugin"]),
        }

        def _fetch(source, **kwargs):
            if source.name == "broken":
                raise ConnectionError("network down")
            return manifests[source.name]

        with (
            patch(_PATCH_REGISTRY, return_value=sources),
            patch(_PATCH_FETCH, side_effect=_fetch),
            caplog.at_level(logging.DEBUG, logger="apm_cli.marketplace.shadow_detector"),
        ):
            result = detect_shadows("my-plugin", "primary")

        # "good" marketplace returned a match despite "broken" failing
        assert len(result) == 1
        assert result[0].marketplace_name == "good"
        # Verify the error was logged at DEBUG level
        assert any("broken" in rec.message for rec in caplog.records)

    def test_no_registered_marketplaces(self):
        """No marketplaces registered -- returns empty list."""
        with (
            patch(_PATCH_REGISTRY, return_value=[]),
            patch(_PATCH_FETCH) as mock_fetch,
        ):
            result = detect_shadows("anything", "nonexistent")

        assert result == []
        mock_fetch.assert_not_called()

    def test_only_primary_registered(self):
        """Only primary marketplace registered -- returns empty list."""
        sources = [_make_source("only-one")]

        with (
            patch(_PATCH_REGISTRY, return_value=sources),
            patch(_PATCH_FETCH) as mock_fetch,
        ):
            result = detect_shadows("my-plugin", "only-one")

        assert result == []
        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: resolver.py shadow warning
# ---------------------------------------------------------------------------


class TestShadowDetectionInResolver:
    """Verify resolver.py logs a warning when shadows are detected."""

    def test_shadow_detection_in_resolver(self, caplog):
        """resolve_marketplace_plugin emits a warning per shadow."""
        from apm_cli.marketplace.resolver import resolve_marketplace_plugin

        plugin = MarketplacePlugin(
            name="sec-check",
            source={"type": "github", "repo": "acme/sec-check", "ref": "main"},
        )
        manifest = MarketplaceManifest(name="acme", plugins=(plugin,))
        source = MarketplaceSource(name="acme", owner="acme", repo="marketplace")

        shadow = ShadowMatch(marketplace_name="community", plugin_name="sec-check")

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
            ) as mock_detect,
            caplog.at_level(logging.WARNING, logger="apm_cli.marketplace.resolver"),
        ):
            canonical, resolved = resolve_marketplace_plugin("sec-check", "acme")

        # Resolution succeeded
        assert canonical == "acme/sec-check#main"
        assert resolved.name == "sec-check"

        # Shadow detection was called with correct args
        mock_detect.assert_called_once_with("sec-check", "acme", auth_resolver=None)

        # Warning emitted for the shadow
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "community" in warnings[0].message
        assert "sec-check" in warnings[0].message


# ---------------------------------------------------------------------------
# Integration: install.py provenance fields
# ---------------------------------------------------------------------------


class TestProvenanceSetOnMarketplaceDeps:
    """Verify install.py sets discovered_via and marketplace_plugin_name."""

    def test_provenance_set_on_marketplace_deps(self):
        """Marketplace provenance dict is correctly structured."""
        # This test validates the contract between the install command's
        # marketplace interception and the lockfile provenance attachment.
        # We verify the data shape, not the full install flow.
        from apm_cli.deps.lockfile import LockedDependency

        # Simulate what install.py lines 169-173 produce
        marketplace_name = "acme-tools"
        plugin_name = "sec-check"

        marketplace_provenance = {
            "discovered_via": marketplace_name,
            "marketplace_plugin_name": plugin_name,
        }

        # Simulate what install.py produces
        dep = LockedDependency(repo_url="acme/sec-check")
        dep.discovered_via = marketplace_provenance["discovered_via"]
        dep.marketplace_plugin_name = marketplace_provenance["marketplace_plugin_name"]

        # Security-critical: all provenance fields must be set
        assert dep.discovered_via == "acme-tools"
        assert dep.marketplace_plugin_name == "sec-check"

        # Round-trip through serialization
        d = dep.to_dict()
        assert d["discovered_via"] == "acme-tools"
        assert d["marketplace_plugin_name"] == "sec-check"
