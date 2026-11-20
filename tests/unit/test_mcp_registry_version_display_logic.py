"""Regression tests for #3437: MCP Registry v0.1 server versions (pure logic cases)."""

from unittest.mock import patch

import pytest

from apm_cli.registry.integration import RegistryIntegration

pytestmark = pytest.mark.unit

_SEARCH = "apm_cli.registry.client.SimpleRegistryClient.search_servers"
_FIND = "apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference"

V01_SERVER = {
    "name": "io.github.github/github-mcp-server",
    "description": "GitHub's official MCP Server",
    "version": "2.0.3",
    "repository": {"url": "https://github.com/github/github-mcp-server", "source": "github"},
}

LEGACY_SERVER = {
    "name": "io.github.acme/legacy-server",
    "description": "Legacy registry record",
    "version_detail": {"version": "1.2.3", "release_date": "2025-01-01", "is_latest": True},
    "repository": {"url": "https://github.com/acme/legacy-server"},
}

UNVERSIONED_SERVER = {
    "name": "io.github.acme/unversioned-server",
    "description": "Record without any version",
    "repository": {"url": "https://github.com/acme/unversioned-server"},
}


@pytest.fixture(autouse=True)
def _isolate_registry_url(monkeypatch):
    monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)


# ---------------------------------------------------------------------------
# RegistryIntegration conversion
# ---------------------------------------------------------------------------


class TestRegistryIntegrationVersion:
    def test_basic_conversion_keeps_v0_1_version(self):
        with patch(_SEARCH, return_value=[dict(V01_SERVER)]):
            results = RegistryIntegration().search_packages("github-mcp-server")

        assert results[0]["version"] == "2.0.3"
        assert "version_detail" not in results[0]

    def test_detailed_conversion_keeps_v0_1_version(self):
        with patch(_FIND, return_value=dict(V01_SERVER)):
            detail = RegistryIntegration().get_package_info(V01_SERVER["name"])

        assert detail["version"] == "2.0.3"
        assert "version_detail" not in detail

    def test_legacy_version_detail_conversion_unchanged(self):
        with patch(_FIND, return_value=dict(LEGACY_SERVER)):
            detail = RegistryIntegration().get_package_info(LEGACY_SERVER["name"])

        assert detail["version_detail"] == LEGACY_SERVER["version_detail"]
        assert detail["versions"] == [
            {"version": "1.2.3", "release_date": "2025-01-01", "is_latest": True}
        ]
        assert "version" not in detail

    def test_unversioned_record_has_no_version(self):
        with patch(_SEARCH, return_value=[dict(UNVERSIONED_SERVER)]):
            results = RegistryIntegration().search_packages("unversioned")

        assert "version" not in results[0]

    @pytest.mark.parametrize("bad_version", [None, "", 3, {"v": "1"}])
    def test_non_string_or_empty_version_is_ignored(self, bad_version):
        server = dict(UNVERSIONED_SERVER, version=bad_version)
        with patch(_SEARCH, return_value=[server]):
            results = RegistryIntegration().search_packages("unversioned")

        assert "version" not in results[0]


# ---------------------------------------------------------------------------
# Rendered command output (real integration, stubbed client fetches)
# ---------------------------------------------------------------------------
