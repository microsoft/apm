"""Regression tests for #3437: MCP Registry v0.1 server versions.

v0.1 records carry the version at ``server.version`` and have no legacy
``version_detail`` object. These tests drive the real
``RegistryIntegration`` conversion (only the registry client fetches are
stubbed, so no network is used) and the rendered ``apm mcp search`` /
``apm mcp show`` output.
"""

import io
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from rich.console import Console
from rich.theme import Theme

from apm_cli.commands.mcp import mcp
from apm_cli.registry.integration import RegistryIntegration

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


def _recording_console():
    return Console(
        file=io.StringIO(),
        record=True,
        width=200,
        force_terminal=False,
        color_system=None,
        theme=Theme({"muted": "dim white"}),
    )


def _run(args, *, search=None, find=None):
    console = _recording_console()
    with (
        patch(_SEARCH, return_value=search or []),
        patch(_FIND, return_value=find),
        patch("apm_cli.commands.mcp._get_console", return_value=console),
    ):
        result = CliRunner().invoke(mcp, args)
    assert result.exit_code == 0, result.output
    return console.export_text()


def _row_cells(text, label):
    """Return the stripped cells of the first table row whose first cell is ``label``."""
    for line in text.splitlines():
        # Rich draws column separators with U+2502 (box drawing vertical).
        cells = [c.strip() for c in line.replace("\u2502", "|").split("|")]
        if len(cells) > 2 and cells[1] == label:
            return cells[1:-1]
    raise AssertionError(f"row {label!r} not found in:\n{text}")


class TestMcpSearchRendersVersion:
    def test_search_shows_v0_1_version_under_latest(self):
        text = _run(["search", "github-mcp-server"], search=[dict(V01_SERVER)])

        row = _row_cells(text, V01_SERVER["name"])
        assert row[-1] == "2.0.3"

    def test_search_legacy_record_renders_as_before(self):
        text = _run(["search", "legacy"], search=[dict(LEGACY_SERVER)])

        row = _row_cells(text, LEGACY_SERVER["name"])
        assert row[-1] == "--"

    def test_search_unversioned_record_keeps_placeholder(self):
        text = _run(["search", "unversioned"], search=[dict(UNVERSIONED_SERVER)])

        row = _row_cells(text, UNVERSIONED_SERVER["name"])
        assert row[-1] == "--"


class TestMcpShowRendersVersion:
    def test_show_displays_v0_1_version(self):
        text = _run(["show", V01_SERVER["name"]], find=dict(V01_SERVER))

        assert _row_cells(text, "Version")[-1] == "2.0.3"

    def test_show_legacy_version_detail_renders_as_before(self):
        text = _run(["show", LEGACY_SERVER["name"]], find=dict(LEGACY_SERVER))

        assert _row_cells(text, "Version")[-1] == "1.2.3"

    def test_show_unversioned_record_keeps_placeholder(self):
        text = _run(["show", UNVERSIONED_SERVER["name"]], find=dict(UNVERSIONED_SERVER))

        assert _row_cells(text, "Version")[-1] == "Unknown"
