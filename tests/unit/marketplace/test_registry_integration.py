"""Tests for the marketplace.json registry-routing extension.

Covers docs/proposals/registry-api.md §4.5:
- New ``registry`` field on plugin entries (semver-validated)
- Backwards-compat: existing marketplace.json files (no ``registry``
  field) parse byte-identically.
"""

from __future__ import annotations

from apm_cli.marketplace.models import (
    parse_marketplace_json,
)

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
