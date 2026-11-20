"""Tests for the install flow with mocked marketplace resolution."""

from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from apm_cli.marketplace.models import MarketplacePlugin, MarketplaceSource
from apm_cli.marketplace.resolver import (
    parse_marketplace_ref,
)

pytestmark = pytest.mark.unit


class TestInstallMarketplacePreParse:
    """The pre-parse intercept in _validate_and_add_packages_to_apm_yml."""

    def test_marketplace_ref_detected(self):
        """NAME@MARKETPLACE triggers marketplace resolution."""
        result = parse_marketplace_ref("security-checks@acme-tools")
        assert result == ("security-checks", "acme-tools", None)

    @pytest.mark.parametrize(
        "raw",
        ["owner/repo", "owner/repo@alias", "just-a-name", "git@github.com:o/r"],
        ids=["owner-repo", "owner-repo-at-alias", "bare-name", "ssh"],
    )
    def test_non_marketplace_ref_not_intercepted(self, raw):
        """owner/repo, slash- or colon-bearing refs and bare names are not marketplace refs."""
        assert parse_marketplace_ref(raw) is None


class TestValidationOutcomeProvenance:
    """Verify marketplace provenance is attached to ValidationOutcome."""

    def test_outcome_has_provenance_field(self):
        from apm_cli.core.command_logger import _ValidationOutcome

        outcome = _ValidationOutcome(
            valid=[("owner/repo", False)],
            invalid=[],
            marketplace_provenance={
                "owner/repo": {
                    "discovered_via": "acme-tools",
                    "marketplace_plugin_name": "security-checks",
                    "source_url": "https://catalog.example.com/marketplace.json",
                    "source_digest": "sha256:" + "a" * 64,
                }
            },
        )
        assert outcome.marketplace_provenance is not None
        assert "owner/repo" in outcome.marketplace_provenance
        source_url = urlparse(outcome.marketplace_provenance["owner/repo"]["source_url"])
        assert (source_url.scheme, source_url.hostname, source_url.path) == (
            "https",
            "catalog.example.com",
            "/marketplace.json",
        )

    def test_outcome_no_provenance(self):
        from apm_cli.core.command_logger import _ValidationOutcome

        outcome = _ValidationOutcome(valid=[], invalid=[])
        assert outcome.marketplace_provenance is None


class TestMarketplaceResolutionProvenance:
    """Resolver carries marketplace source provenance to install validation."""

    @patch("apm_cli.marketplace.resolver.fetch_or_cache")
    @patch("apm_cli.marketplace.resolver.get_marketplace_by_name")
    def test_resolution_includes_manifest_source_url_and_digest(self, mock_get_source, mock_fetch):
        from apm_cli.marketplace.models import MarketplaceManifest
        from apm_cli.marketplace.resolver import resolve_marketplace_plugin

        mock_get_source.return_value = MarketplaceSource(
            name="catalog",
            url="https://catalog.example.com/marketplace.json",
            path="",
        )
        mock_fetch.return_value = MarketplaceManifest(
            name="catalog",
            plugins=(
                MarketplacePlugin(name="tool", source={"type": "github", "repo": "acme/tool"}),
            ),
            source_url="https://catalog.example.com/marketplace.json",
            source_digest="sha256:" + "d" * 64,
        )

        resolution = resolve_marketplace_plugin("tool", "catalog")

        resolved_source = urlparse(resolution.source_url)
        assert (resolved_source.scheme, resolved_source.hostname, resolved_source.path) == (
            "https",
            "catalog.example.com",
            "/marketplace.json",
        )
        assert resolution.source_digest == "sha256:" + "d" * 64
