"""Regression tests for marketplace catalog discovery reuse (microsoft/apm#3283).

``apm marketplace add`` used to fetch the winning catalog during path
discovery and then immediately re-fetch the identical URL through
``fetch_marketplace(force_refresh=True)``.  The approved bounded repair
threads the discovery response through so registration consumes it without
a duplicate successful fetch, while the sidecar-cache write (and therefore
explicit catalog freshness on later invocations) is preserved.
"""

from __future__ import annotations

from unittest.mock import patch

from apm_cli.marketplace import client as mp_client
from apm_cli.marketplace.models import MarketplaceSource

CATALOG = {"name": "catalog", "plugins": [{"name": "skill-creator", "source": "owner/repo"}]}


def _github_source(path: str = "marketplace.json") -> MarketplaceSource:
    return MarketplaceSource(
        name="catalog",
        url="https://github.com/owner/repo",
        ref="v1.0.0",
        path=path,
    )


class TestAutoDetectPathWithPayload:
    def test_returns_path_and_payload_for_first_hit(self) -> None:
        with patch.object(
            mp_client,
            "_fetch_file",
            side_effect=lambda source, path, **kw: (
                CATALOG if path == ".claude-plugin/marketplace.json" else None
            ),
        ) as fetch_mock:
            detected, payload = mp_client._auto_detect_path_with_payload(_github_source())

        assert detected == ".claude-plugin/marketplace.json"
        assert payload == CATALOG
        assert fetch_mock.call_count == 3  # first two candidates miss

    def test_returns_none_none_when_no_candidate_exists(self) -> None:
        with patch.object(mp_client, "_fetch_file", return_value=None):
            detected, payload = mp_client._auto_detect_path_with_payload(_github_source())

        assert detected is None
        assert payload is None

    def test_auto_detect_path_still_returns_path_only(self) -> None:
        """Backward-compat wrapper keeps its contract."""
        with patch.object(
            mp_client,
            "_fetch_file",
            side_effect=lambda source, path, **kw: CATALOG if path == "marketplace.json" else None,
        ):
            assert mp_client._auto_detect_path(_github_source()) == "marketplace.json"


class TestFetchMarketplaceDiscoveryPayload:
    def test_discovery_payload_skips_duplicate_fetch(self) -> None:
        source = _github_source()

        with (
            patch.object(mp_client, "_fetch_file") as fetch_mock,
            patch.object(mp_client, "_write_cache") as write_mock,
        ):
            manifest = mp_client.fetch_marketplace(
                source,
                force_refresh=True,
                discovery_payload=CATALOG,
            )

        fetch_mock.assert_not_called()
        write_mock.assert_called_once()
        assert manifest.name == "catalog"
        assert [p.name for p in manifest.plugins] == ["skill-creator"]

    def test_discovery_payload_none_fetches_normally(self) -> None:
        source = _github_source()

        with (
            patch.object(mp_client, "_fetch_file", return_value=CATALOG) as fetch_mock,
            patch.object(mp_client, "_write_cache"),
        ):
            manifest = mp_client.fetch_marketplace(source, force_refresh=True)

        fetch_mock.assert_called_once()
        assert manifest.name == "catalog"

    def test_discovery_payload_still_writes_sidecar_cache(self) -> None:
        """Freshness behavior on later invocations is preserved."""
        import json
        import os

        source = _github_source()

        with (
            patch.object(mp_client, "_fetch_file") as fetch_mock,
            patch.object(mp_client, "_cache_dir", return_value=mp_client._cache_dir()),
        ):
            # Real cache write under the test's HOME (see conftest), then a
            # second fetch must be served from the fresh sidecar cache.
            mp_client.fetch_marketplace(source, force_refresh=True, discovery_payload=CATALOG)
            fetch_mock.assert_not_called()

            cache_path = mp_client._cache_data_path(mp_client._cache_key(source))
            with open(cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
            assert data == CATALOG

            mp_client.fetch_marketplace(source)  # no force_refresh -> cache hit
            fetch_mock.assert_not_called()

        assert os.path.exists(mp_client._cache_data_path(mp_client._cache_key(source)))
