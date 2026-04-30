"""Tests for apm_cli.models.plugin module I/O.

Round-trips non-ASCII content through Plugin.from_path to guard against
cp1252/cp950 UnicodeDecodeError on Windows when reading plugin.json.
"""

import json

from apm_cli.models.plugin import Plugin


class TestPluginUtf8RoundTrip:
    """Round-trip non-ASCII content through plugin.json reads."""

    def test_from_path_reads_non_ascii_metadata(self, tmp_path):
        metadata = {
            "id": "demo-plugin",
            "name": "Demo plugin -- cafe",
            "version": "1.0.0",
            "description": "Plugin de demo with 中文 description",
            "author": "Cafe Author",
        }
        plugin_json = tmp_path / "plugin.json"
        plugin_json.write_bytes(json.dumps(metadata).encode("utf-8"))

        plugin = Plugin.from_path(tmp_path)

        assert plugin.metadata.id == "demo-plugin"
        assert plugin.metadata.name == "Demo plugin -- cafe"
        assert plugin.metadata.description == "Plugin de demo with 中文 description"
        assert plugin.metadata.author == "Cafe Author"
