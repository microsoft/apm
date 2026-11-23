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


class TestCatalogScopedRefPins:
    """#4966: ref pins belong to the catalog, not to the alias that named it."""

    @pytest.fixture(autouse=True)
    def _pins_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APM_HOME", str(tmp_path / "apm-home"))
        self.pins = tmp_path / "apm-home" / "cache" / "marketplace" / "version-pins.json"
        self.pins.parent.mkdir(parents=True)

    def _write(self, pins: dict) -> None:
        self.pins.write_text(json.dumps(pins))

    def _pinned(self) -> dict:
        return json.loads(self.pins.read_text())

    @staticmethod
    def _key(source: MarketplaceSource) -> str:
        from apm_cli.marketplace.source_identity import catalog_pin_identity

        return f"{catalog_pin_identity(source)}#tool/1.0.0"

    @staticmethod
    def _check(alias: str, ref: str, source: MarketplaceSource):
        from apm_cli.marketplace.source_identity import catalog_pin_identity
        from apm_cli.marketplace.version_pins import check_ref_pin

        return check_ref_pin(alias, "tool", ref, "1.0.0", catalog=catalog_pin_identity(source))

    def test_a_legacy_alias_pin_still_guards_the_catalog_the_alias_names(self):
        self._write({"team/tool/1.0.0": "v1"})
        source = MarketplaceSource(name="team", owner="a", repo="catalog")
        registry_mod.add_marketplace(source)

        assert self._check("team", "v2", source) == "v1"
        assert self._pinned() == {"team/tool/1.0.0": "v1"}

    def test_removing_an_alias_keeps_its_catalog_pinned(self):
        self._write({"team/tool/1.0.0": "v1", "teams/tool/1.0.0": "v9"})
        source = MarketplaceSource(name="team", owner="a", repo="catalog")
        registry_mod.add_marketplace(source)

        registry_mod.remove_marketplace("TEAM")
        registry_mod.add_marketplace(MarketplaceSource(name="other", owner="a", repo="catalog"))

        assert self._check("other", "v2", source) == "v1"
        assert json.loads(self.pins.read_text())["teams/tool/1.0.0"] == "v9"

    def test_an_alias_re_added_for_another_catalog_starts_without_its_pins(self):
        self._write({"team/tool/1.0.0": "v1"})
        registry_mod.add_marketplace(MarketplaceSource(name="team", owner="a", repo="catalog"))

        registry_mod.remove_marketplace("team")
        other = MarketplaceSource(name="team", owner="b", repo="catalog")
        registry_mod.add_marketplace(other)

        assert self._check("team", "v2", other) is None
        first = MarketplaceSource(name="team", owner="a", repo="catalog")
        assert self._pinned() == {self._key(first): "v1"}

    def test_rebinding_an_alias_in_place_keeps_the_old_catalog_pinned(self):
        self._write({"team/tool/1.0.0": "v1"})
        old = MarketplaceSource(name="team", owner="a", repo="catalog")
        registry_mod.add_marketplace(old)

        new = MarketplaceSource(name="team", owner="b", repo="catalog")
        registry_mod.add_marketplace(new)

        assert self._check("team", "v2", new) is None
        assert self._check("team", "v2", old) == "v1"
        assert self._pinned() == {self._key(old): "v1"}

    @pytest.mark.parametrize(
        "spelling",
        [
            "https://github.com/a/catalog.git",
            "git@github.com:a/catalog.git",
            "https://GitHub.com/a/catalog.git",
        ],
    )
    def test_an_equivalent_catalog_spelling_keeps_its_pins(self, spelling: str):
        self._write({"team/tool/1.0.0": "v1"})
        registry_mod.add_marketplace(
            MarketplaceSource(name="team", url="https://github.com/a/catalog")
        )

        respelled = MarketplaceSource(name="team", url=spelling)
        registry_mod.add_marketplace(respelled)

        assert self._check("team", "v2", respelled) == "v1"
        assert self._pinned() == {self._key(respelled): "v1"}

    def test_a_manifest_removal_keeps_its_catalog_pinned(self):
        self._write({"team/tool/1.0.0": "v1"})
        source = MarketplaceSource(name="team", owner="a", repo="catalog", origin="manifest")
        registry_mod.register_manifest_marketplace(source)

        assert registry_mod.unregister_manifest_marketplace("team")

        assert self._check("team", "v2", source) == "v1"
        assert "team/tool/1.0.0" not in json.loads(self.pins.read_text())

    def test_an_alias_named_like_a_host_never_adopts_catalog_scoped_pins(self):
        scoped = "github.com/a/catalog:marketplace.json#tool/1.0.0"
        self._write({scoped: "v1", "github.com/tool/1.0.0": "v0"})
        registry_mod.add_marketplace(
            MarketplaceSource(name="github.com", url="https://github.com/b/catalog")
        )

        registry_mod.remove_marketplace("github.com")

        pins = json.loads(self.pins.read_text())
        assert pins[scoped] == "v1"
        assert "github.com/tool/1.0.0" not in pins
        assert pins["github.com/b/catalog:marketplace.json#tool/1.0.0"] == "v0"


class TestCatalogPinIdentity:
    """#4966: the pin scope names exactly one catalog."""

    @staticmethod
    def _record(pins_dir: Path, source: MarketplaceSource) -> dict:
        from apm_cli.marketplace.source_identity import catalog_pin_identity
        from apm_cli.marketplace.version_pins import record_ref_pin

        record_ref_pin(
            "t", "tool", "v1", "1.0.0", str(pins_dir), catalog=catalog_pin_identity(source)
        )
        return json.loads((pins_dir / "version-pins.json").read_text())

    def test_catalog_paths_that_differ_only_in_case_are_different_catalogs(self, tmp_path):
        url = "https://git.example.com/Org/catalog"
        self._record(tmp_path, MarketplaceSource(name="t", url=url, path="catalogs/A.json"))
        pins = self._record(tmp_path, MarketplaceSource(name="t", url=url, path="catalogs/a.json"))

        assert len(pins) == 2

    def test_a_local_catalog_is_scoped_by_its_resolved_location_and_path(self, tmp_path):
        root = tmp_path / "catalogs"
        root.mkdir()
        (tmp_path / "link").symlink_to(root, target_is_directory=True)
        pins_dir = tmp_path / "pins"

        self._record(pins_dir, MarketplaceSource(name="t", url=str(root), path="a.json"))
        self._record(
            pins_dir, MarketplaceSource(name="t", url=str(tmp_path / "link"), path="a.json")
        )
        pins = self._record(pins_dir, MarketplaceSource(name="t", url=str(root), path="b.json"))

        assert len(pins) == 2
