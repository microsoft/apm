"""Tests for marketplace registry CRUD with tmp_path isolation."""

import json  # noqa: F401

import pytest

from apm_cli.marketplace import registry as registry_mod
from apm_cli.marketplace.errors import MarketplaceNotFoundError
from apm_cli.marketplace.models import MarketplaceSource


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    """Isolate registry reads/writes to a temp directory."""
    config_dir = str(tmp_path / ".apm")
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    yield


class TestRegistryBasicOps:
    """CRUD operations on marketplace registry."""

    def test_empty_registry(self):
        assert registry_mod.get_registered_marketplaces() == []
        assert registry_mod.marketplace_count() == 0

    def test_add_and_get(self):
        src = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        registry_mod.add_marketplace(src)
        assert registry_mod.marketplace_count() == 1

        fetched = registry_mod.get_marketplace_by_name("acme")
        assert fetched.name == "acme"
        assert fetched.owner == "acme-org"

    def test_add_replaces_same_name(self):
        src1 = MarketplaceSource(name="acme", owner="old-org", repo="plugins")
        src2 = MarketplaceSource(name="acme", owner="new-org", repo="plugins")
        registry_mod.add_marketplace(src1)
        registry_mod.add_marketplace(src2)
        assert registry_mod.marketplace_count() == 1
        assert registry_mod.get_marketplace_by_name("acme").owner == "new-org"

    def test_add_case_insensitive_replace(self):
        src1 = MarketplaceSource(name="Acme", owner="old", repo="r")
        src2 = MarketplaceSource(name="acme", owner="new", repo="r")
        registry_mod.add_marketplace(src1)
        registry_mod.add_marketplace(src2)
        assert registry_mod.marketplace_count() == 1

    def test_remove(self):
        src = MarketplaceSource(name="acme", owner="o", repo="r")
        registry_mod.add_marketplace(src)
        registry_mod.remove_marketplace("acme")
        assert registry_mod.marketplace_count() == 0

    def test_remove_not_found(self):
        with pytest.raises(MarketplaceNotFoundError):
            registry_mod.remove_marketplace("nonexistent")

    def test_get_not_found(self):
        with pytest.raises(MarketplaceNotFoundError):
            registry_mod.get_marketplace_by_name("nonexistent")

    def test_marketplace_names(self):
        registry_mod.add_marketplace(MarketplaceSource(name="beta", owner="o", repo="r"))
        registry_mod.add_marketplace(MarketplaceSource(name="alpha", owner="o", repo="r"))
        assert registry_mod.marketplace_names() == ["alpha", "beta"]


class TestRegistryPersistence:
    """Verify data survives cache invalidation."""

    def test_persists_to_disk(self, tmp_path):
        src = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        registry_mod.add_marketplace(src)

        # Invalidate cache
        registry_mod._invalidate_cache()

        # Should reload from disk
        fetched = registry_mod.get_marketplace_by_name("acme")
        assert fetched.owner == "acme-org"

    def test_corrupted_file_returns_empty(self, tmp_path):
        path = registry_mod._ensure_file()
        with open(path, "w") as f:
            f.write("not json")

        registry_mod._invalidate_cache()
        assert registry_mod.get_registered_marketplaces() == []


class TestRegistryUtf8RoundTrip:
    """Registry persistence preserves non-ASCII content (Windows cp1252/cp950 guard)."""

    def test_add_and_read_non_ascii_marketplace(self):
        # Note: name/owner/repo are typically ASCII per the marketplace spec,
        # but the registry file itself must still be UTF-8 to handle any
        # non-ASCII content that may flow through future fields. We use a
        # description-bearing source by writing a custom entry directly.
        src = MarketplaceSource(name="cafe-mkt", owner="cafe-org", repo="plugins-\u958b\u59cb")
        registry_mod.add_marketplace(src)

        # Force re-load from disk by clearing the cache.
        registry_mod._invalidate_cache()
        fetched = registry_mod.get_marketplace_by_name("cafe-mkt")
        assert fetched is not None
        assert fetched.repo == "plugins-\u958b\u59cb"

    def test_registry_file_is_readable_with_utf8_external_writes(self, tmp_path):
        """A registry file written externally with raw UTF-8 (ensure_ascii=False)
        must still load cleanly. This is the regression case for cp1252/cp950
        Windows locales where the default open() would fail to decode."""
        import json as _json

        path = registry_mod._marketplaces_path()
        # Ensure parent dir exists.
        registry_mod._ensure_file()
        payload = {
            "marketplaces": [
                {"name": "cafe-mkt", "owner": "o", "repo": "repo-\u4e2d\u6587"},
            ]
        }
        # Write raw UTF-8 (no \uXXXX escaping) to mimic what a non-Python
        # tool or a future writer with ensure_ascii=False would produce.
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False)

        registry_mod._invalidate_cache()
        fetched = registry_mod.get_marketplace_by_name("cafe-mkt")
        assert fetched is not None
        assert fetched.repo == "repo-\u4e2d\u6587"
