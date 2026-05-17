"""Tests for the marketplace.json registry-routing extension and the
server-side registry search merge.

Covers docs/proposals/registry-api.md §4.5:
- New ``registry`` field on plugin entries (semver-validated)
- Backwards-compat: existing marketplace.json files (no ``registry``
  field) parse byte-identically.
- ``search_all_registries`` adapter wraps RegistryClient.search results
  into MarketplacePlugin so callers can merge them with marketplace
  search results.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.registry.client import RegistryClient, SearchResult
from apm_cli.marketplace.client import search_all_registries
from apm_cli.marketplace.models import (
    MarketplacePlugin,
    parse_marketplace_json,
)


@pytest.fixture(autouse=True)
def _enable_package_registry(monkeypatch):
    """Most tests in this module exercise registry-backed marketplace search."""
    import apm_cli.config as _conf

    monkeypatch.setattr(
        _conf,
        "_config_cache",
        {"experimental": {"package_registry": True}},
    )


def _disable_package_registry(monkeypatch):
    import apm_cli.config as _conf

    monkeypatch.setattr(_conf, "_config_cache", {"experimental": {}})


# ───────────────────────────────────────────────────────────────────────────
# Schema extension: ``registry`` field on plugin entries
# ───────────────────────────────────────────────────────────────────────────


class TestRegistryFieldParsing:
    def test_plugin_without_registry_field_unchanged(self):
        # Sanity: existing marketplace.json shape parses with registry="".
        manifest = parse_marketplace_json(
            {
                "name": "acme",
                "plugins": [
                    {
                        "name": "review",
                        "repository": "acme/review",
                        "description": "x",
                        "version": "v1.0",
                    }
                ],
            },
            source_name="acme",
        )
        plugin = manifest.plugins[0]
        assert plugin.name == "review"
        assert plugin.version == "v1.0"
        assert plugin.registry == ""

    def test_plugin_with_valid_registry_routing(self):
        manifest = parse_marketplace_json(
            {
                "name": "acme",
                "plugins": [
                    {
                        "name": "enterprise-skills",
                        "repository": "acme/enterprise-skills",
                        "registry": "corp-main",
                        "version": "^3.0.0",
                        "description": "x",
                    }
                ],
            },
            source_name="acme",
        )
        plugin = manifest.plugins[0]
        assert plugin.registry == "corp-main"
        assert plugin.version == "^3.0.0"

    def test_invalid_registry_field_downgrades_silently(self):
        # Empty string, non-string, or invalid types: log + downgrade.
        manifest = parse_marketplace_json(
            {
                "name": "acme",
                "plugins": [
                    {
                        "name": "x",
                        "repository": "a/b",
                        "registry": 123,  # not a string
                    }
                ],
            },
            source_name="acme",
        )
        assert manifest.plugins[0].registry == ""

    def test_invalid_semver_disables_registry_routing(self):
        # If registry is set but version isn't a semver range, drop the
        # routing to "" so the plugin doesn't silently mis-resolve.
        manifest = parse_marketplace_json(
            {
                "name": "acme",
                "plugins": [
                    {
                        "name": "x",
                        "repository": "a/b",
                        "registry": "corp",
                        "version": "main",  # branch, not semver
                    }
                ],
            },
            source_name="acme",
        )
        assert manifest.plugins[0].registry == ""

    def test_registry_with_no_version(self):
        # Registry set, no version: parser keeps registry routing but
        # leaves version="" — the resolver will reject it later. This
        # matches "fail at resolve time, not at parse time" since the
        # marketplace parser is permissive by design.
        manifest = parse_marketplace_json(
            {
                "name": "acme",
                "plugins": [
                    {
                        "name": "x",
                        "repository": "a/b",
                        "registry": "corp",
                    }
                ],
            },
            source_name="acme",
        )
        plugin = manifest.plugins[0]
        assert plugin.registry == "corp"
        assert plugin.version == ""

    def test_existing_source_field_unchanged_alongside_registry(self):
        # The new ``registry`` field MUST NOT collide with the existing
        # source-location semantics. Both fields can coexist on one entry.
        manifest = parse_marketplace_json(
            {
                "name": "acme",
                "plugins": [
                    {
                        "name": "x",
                        "source": {"type": "github", "repo": "a/b"},
                        "registry": "corp",
                        "version": "^1.0.0",
                    }
                ],
            },
            source_name="acme",
        )
        plugin = manifest.plugins[0]
        assert plugin.source == {"type": "github", "repo": "a/b"}
        assert plugin.registry == "corp"


# ───────────────────────────────────────────────────────────────────────────
# Server-side registry search merge
# ───────────────────────────────────────────────────────────────────────────


class TestSearchAllRegistries:
    def test_empty_registries_returns_empty(self):
        assert search_all_registries("foo", {}) == []

    def test_non_empty_registries_require_flag(self, monkeypatch):
        _disable_package_registry(monkeypatch)
        with pytest.raises(
            ValueError, match="apm experimental enable package-registry"
        ):
            search_all_registries("foo", {"corp": "https://corp.example.com"})

    def test_calls_each_configured_registry(self):
        with patch("apm_cli.deps.registry.client.RegistryClient") as MockClient:
            instance_a = MagicMock(spec=RegistryClient)
            instance_a.search.return_value = [
                SearchResult(
                    id="acme/skill-a",
                    latest_version="1.0.0",
                    description="dA",
                    author="a",
                    tags=["security"],
                    type="skill",
                    score=0.9,
                )
            ]
            instance_b = MagicMock(spec=RegistryClient)
            instance_b.search.return_value = [
                SearchResult(
                    id="acme/skill-b",
                    latest_version="2.0.0",
                    description="dB",
                    author="b",
                    tags=[],
                    type="skill",
                    score=0.8,
                )
            ]
            MockClient.side_effect = [instance_a, instance_b]

            results = search_all_registries(
                "skill",
                {
                    "corp-a": "https://a.example.com/apm",
                    "corp-b": "https://b.example.com/apm",
                },
            )
            ids = [r.name for r in results]
            assert "acme/skill-a" in ids
            assert "acme/skill-b" in ids
            # Each result is annotated with its source registry
            for r in results:
                assert r.registry in ("corp-a", "corp-b")
                assert r.source_marketplace == r.registry

    def test_skips_registry_on_error(self):
        from apm_cli.deps.registry.client import RegistryError

        with patch("apm_cli.deps.registry.client.RegistryClient") as MockClient:
            failing = MagicMock(spec=RegistryClient)
            failing.search.side_effect = RegistryError(
                "boom", status=500, url="https://failing"
            )
            ok = MagicMock(spec=RegistryClient)
            ok.search.return_value = [
                SearchResult(
                    id="acme/x",
                    latest_version="1.0.0",
                    description=None,
                    author=None,
                    tags=[],
                    type=None,
                    score=None,
                )
            ]
            MockClient.side_effect = [failing, ok]

            results = search_all_registries(
                "skill",
                {
                    "failing-corp": "https://failing.example.com",
                    "good-corp": "https://good.example.com",
                },
            )
            # The failing registry is skipped; the good one yields results.
            ids = [r.name for r in results]
            assert "acme/x" in ids

    def test_results_are_marketplace_plugin_shaped(self):
        with patch("apm_cli.deps.registry.client.RegistryClient") as MockClient:
            client = MagicMock(spec=RegistryClient)
            client.search.return_value = [
                SearchResult(
                    id="acme/foo",
                    latest_version="1.2.3",
                    description="desc",
                    author="me",
                    tags=["a", "b"],
                    type="prompt",
                    score=0.5,
                )
            ]
            MockClient.return_value = client

            results = search_all_registries("foo", {"corp": "https://corp.example.com"})
            assert len(results) == 1
            r = results[0]
            assert isinstance(r, MarketplacePlugin)
            assert r.name == "acme/foo"
            assert r.version == "1.2.3"
            assert r.description == "desc"
            assert r.tags == ("a", "b")
            assert r.registry == "corp"

    def test_passes_filter_params_through(self):
        with patch("apm_cli.deps.registry.client.RegistryClient") as MockClient:
            client = MagicMock(spec=RegistryClient)
            client.search.return_value = []
            MockClient.return_value = client

            search_all_registries(
                "x",
                {"corp": "https://corp.example.com"},
                limit=10,
                type="skill",
                tag="security",
            )
            client.search.assert_called_once_with(
                "x", limit=10, package_type="skill", tag="security"
            )
