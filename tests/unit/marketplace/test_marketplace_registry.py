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
