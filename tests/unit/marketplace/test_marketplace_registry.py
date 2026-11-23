"""Tests for marketplace registry CRUD with tmp_path isolation."""

import json
from pathlib import Path

import pytest

from apm_cli.marketplace import registry as registry_mod
from apm_cli.marketplace.errors import MarketplaceNotFoundError
from apm_cli.marketplace.models import MarketplaceSource

pytestmark = pytest.mark.component


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    """Isolate registry reads/writes to a temp directory."""
    config_dir = str(tmp_path / ".apm")
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    yield


def _registry_file() -> Path:
    """The registry path with its parent directory created (no registry file)."""
    path = Path(registry_mod._marketplaces_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TestRegistryBasicOps:
    """CRUD operations on marketplace registry."""

    def test_empty_registry(self):
        assert registry_mod.get_registered_marketplaces() == []
        assert registry_mod.marketplace_count() == 0

    def test_reads_never_create_the_registry(self):
        """A missing registry reads as empty and nothing is written (#4600)."""
        config_dir = Path(registry_mod._marketplaces_path()).parent
        assert registry_mod.get_registered_marketplaces() == []
        assert registry_mod.marketplace_names() == []
        with pytest.raises(MarketplaceNotFoundError):
            registry_mod.get_marketplace_by_name("acme")
        assert not config_dir.exists()

        registry_mod.add_marketplace(MarketplaceSource(name="acme", owner="o", repo="r"))
        written = json.loads(Path(registry_mod._marketplaces_path()).read_text())
        assert [entry["name"] for entry in written["marketplaces"]] == ["acme"]

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

    def test_failed_same_alias_replacement_preserves_original_registry_bytes(self):
        registry_mod.add_marketplace(
            MarketplaceSource(
                name="acme",
                url="ssh://git@[2001:db8::1]:2222/Team/Marketplace.git",
            )
        )
        path = registry_mod._marketplaces_path()
        with open(path, "rb") as registry_file:
            before = registry_file.read()

        with pytest.raises(ValueError):
            MarketplaceSource(
                name="acme",
                url="ssh://git:secret@[2001:db8::1]:2222/Team/Marketplace.git",
            )

        with open(path, "rb") as registry_file:
            assert registry_file.read() == before
        registry_mod._invalidate_cache()
        restored = registry_mod.get_marketplace_by_name("acme")
        assert restored.url == "ssh://git@[2001:db8::1]:2222/Team/Marketplace.git"

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
        path = _registry_file()
        with open(path, "w") as f:
            f.write("not json")

        registry_mod._invalidate_cache()
        assert registry_mod.get_registered_marketplaces() == []

    def test_invalid_persisted_source_does_not_hide_valid_entries(self):
        path = _registry_file()
        path.write_text(
            json.dumps(
                {
                    "marketplaces": [
                        {
                            "name": "valid",
                            "url": "https://github.com/acme/marketplace",
                        },
                        {
                            "name": "invalid",
                            "url": "ssh:///missing-host.git",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        registry_mod._invalidate_cache()
        sources = registry_mod.get_registered_marketplaces()

        assert [source.name for source in sources] == ["valid"]


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

        path = _registry_file()
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


class TestAliasRefPins:
    """#4966: an alias's ref pins leave with the catalog they were recorded for."""

    @pytest.fixture(autouse=True)
    def _pins_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APM_HOME", str(tmp_path / "apm-home"))
        self.pins = tmp_path / "apm-home" / "cache" / "marketplace" / "version-pins.json"
        self.pins.parent.mkdir(parents=True)
        self.pins.write_text(
            json.dumps({"team/tool/1.0.0": "v1", "team/other": "main", "teams/tool": "v9"})
        )

    def _pins(self) -> dict:
        return json.loads(self.pins.read_text())

    def test_remove_withdraws_only_that_alias_pins(self):
        registry_mod.add_marketplace(MarketplaceSource(name="team", owner="a", repo="catalog"))

        registry_mod.remove_marketplace("TEAM")

        assert self._pins() == {"teams/tool": "v9"}

    def test_rebinding_an_alias_to_another_catalog_withdraws_its_pins(self):
        registry_mod.add_marketplace(MarketplaceSource(name="team", owner="a", repo="catalog"))

        registry_mod.add_marketplace(MarketplaceSource(name="team", owner="b", repo="catalog"))

        assert self._pins() == {"teams/tool": "v9"}

    def test_re_adding_the_same_catalog_keeps_its_pins(self):
        registry_mod.add_marketplace(MarketplaceSource(name="team", owner="a", repo="catalog"))
        before = self._pins()

        registry_mod.add_marketplace(
            MarketplaceSource(name="team", owner="a", repo="catalog", ref="release")
        )

        assert self._pins() == before

    def test_manifest_removal_withdraws_the_alias_pins(self):
        registry_mod.register_manifest_marketplace(
            MarketplaceSource(name="team", owner="a", repo="catalog", origin="manifest")
        )

        assert registry_mod.unregister_manifest_marketplace("team")

        assert self._pins() == {"teams/tool": "v9"}
