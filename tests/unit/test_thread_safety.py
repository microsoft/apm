"""Thread-safety tests for console singleton and marketplace registry lock."""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.utils.console import _get_console, _reset_console


# ---------------------------------------------------------------------------
# Console singleton tests
# ---------------------------------------------------------------------------


class TestConsoleSingleton:
    """Verify _get_console() returns a thread-safe singleton."""

    def setup_method(self):
        _reset_console()

    def teardown_method(self):
        _reset_console()

    def test_console_singleton_returns_same_instance(self):
        """Two sequential calls return the exact same object."""
        first = _get_console()
        second = _get_console()
        assert first is not None
        assert first is second

    def test_console_singleton_thread_safe(self):
        """10 threads all receive the same Console instance."""
        results: list = [None] * 10
        barrier = threading.Barrier(10)

        def _worker(idx: int) -> None:
            barrier.wait()
            results[idx] = _get_console()

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All results must be the same object
        assert all(r is not None for r in results)
        assert all(r is results[0] for r in results)

    def test_console_reset_clears_singleton(self):
        """_reset_console() forces a fresh instance on next call."""
        first = _get_console()
        assert first is not None

        _reset_console()

        second = _get_console()
        assert second is not None
        assert second is not first


# ---------------------------------------------------------------------------
# Marketplace registry lock tests
# ---------------------------------------------------------------------------


class TestRegistryThreadSafety:
    """Verify registry cache operations are safe under concurrent access."""

    @pytest.fixture(autouse=True)
    def _isolate_registry(self, tmp_path, monkeypatch):
        """Point registry at a temp directory so tests never touch real config."""
        config_dir = str(tmp_path / ".apm")
        monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)
        monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
        monkeypatch.setattr(
            "apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json")
        )
        monkeypatch.setattr("apm_cli.config._config_cache", None)
        self._tmp_path = tmp_path
        yield

    def _seed_marketplace_file(self, entries: list) -> None:
        """Write a marketplaces.json with the given entries."""
        import os

        config_dir = str(self._tmp_path / ".apm")
        os.makedirs(config_dir, exist_ok=True)
        path = os.path.join(config_dir, "marketplaces.json")
        with open(path, "w") as f:
            json.dump({"marketplaces": entries}, f)

    def test_registry_cache_thread_safe(self):
        """Concurrent _invalidate_cache + _load must not crash."""
        from apm_cli.marketplace import registry as reg

        self._seed_marketplace_file(
            [{"name": "acme", "owner": "o", "repo": "r"}]
        )

        errors: list = []
        barrier = threading.Barrier(10)

        def _worker() -> None:
            try:
                barrier.wait()
                reg._invalidate_cache()
                result = reg._load()
                assert isinstance(result, list)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Threads raised errors: {errors}"

    def test_registry_load_under_lock(self):
        """After _load(), _registry_cache is populated."""
        from apm_cli.marketplace import registry as reg

        self._seed_marketplace_file(
            [{"name": "tools", "owner": "org", "repo": "repo"}]
        )

        result = reg._load()
        assert len(result) == 1
        assert result[0].name == "tools"

        # Cache should now be populated (non-None)
        with reg._registry_lock:
            assert reg._registry_cache is not None
            assert len(reg._registry_cache) == 1

    def test_registry_invalidate_clears_cache(self):
        """_load() then _invalidate_cache() causes next _load() to re-read."""
        from apm_cli.marketplace import registry as reg

        self._seed_marketplace_file(
            [{"name": "v1", "owner": "o", "repo": "r"}]
        )

        first = reg._load()
        assert len(first) == 1
        assert first[0].name == "v1"

        # Overwrite the file with different data
        self._seed_marketplace_file(
            [
                {"name": "v2", "owner": "o2", "repo": "r2"},
                {"name": "v3", "owner": "o3", "repo": "r3"},
            ]
        )

        # Without invalidation the cache returns stale data
        stale = reg._load()
        assert len(stale) == 1  # still cached

        # Invalidate → next load reads from disk
        reg._invalidate_cache()
        fresh = reg._load()
        assert len(fresh) == 2
        names = {s.name for s in fresh}
        assert names == {"v2", "v3"}
