"""Unit tests for plugin_parser.py and find_plugin_json helper (pure logic cases)."""

import pytest
import yaml

from apm_cli.deps.plugin_parser import (
    _generate_apm_yml,
    _union_dep_list,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-legacy-plugin-skill-membership")]


def test_union_dep_list_indexes_existing_entries_once() -> None:
    """Large dependency merges must not scan the growing result per append."""

    class NoContainsList(list):
        def __contains__(self, _item: object) -> bool:
            raise AssertionError("linear membership scan used")

    existing = NoContainsList(
        {"name": f"server-{index}", "command": "echo"} for index in range(100)
    )
    merged = {"mcp": existing}
    new_entries = [{"name": f"server-{index}", "command": "echo"} for index in range(100, 1000)]
    new_entries.extend(
        [
            {"name": "server-0", "command": "echo"},
            {"name": "server-0", "command": "different"},
        ]
    )

    _union_dep_list(merged, "mcp", new_entries)

    assert len(existing) == 1001
    assert existing[-1] == {"name": "server-0", "command": "different"}


class TestGenerateApmYml:
    def test_generate_full_metadata(self):
        manifest = {
            "name": "full-plugin",
            "version": "2.0.0",
            "description": "Full featured",
            "author": "Bob",
            "license": "Apache-2.0",
            "repository": "https://github.com/org/repo",
            "homepage": "https://example.com",
            "tags": ["ai", "copilot"],
        }

        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)

        assert parsed["name"] == "full-plugin"
        assert parsed["version"] == "2.0.0"
        assert parsed["description"] == "Full featured"
        assert parsed["author"] == "Bob"
        assert parsed["license"] == "Apache-2.0"
        assert parsed["tags"] == ["ai", "copilot"]
        assert parsed["type"] == "hybrid"

    def test_generate_minimal_metadata(self):
        manifest = {"name": "minimal"}

        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)

        assert parsed["name"] == "minimal"
        assert parsed["version"] == "0.0.0"
        assert parsed["description"] == ""
        assert parsed["type"] == "hybrid"

    def test_generate_author_as_dict(self):
        manifest = {
            "name": "dict-author",
            "author": {"name": "Foo Bar", "email": "foo@bar.com"},
        }

        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)

        assert parsed["author"] == "Foo Bar"

    def test_generate_with_dependencies(self):
        manifest = {
            "name": "with-deps",
            "dependencies": {"dep-a": "^1.0", "dep-b": "~2.0"},
        }

        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)

        assert parsed["dependencies"] == {"apm": {"dep-a": "^1.0", "dep-b": "~2.0"}}


class TestGenerateApmYmlMCPDeps:
    """Test _mcp_deps injection in generated apm.yml."""

    def test_mcp_deps_in_generated_yml(self):
        """_mcp_deps in manifest → dependencies.mcp in output."""
        manifest = {
            "name": "mcp-plugin",
            "_mcp_deps": [
                {"name": "my-srv", "registry": False, "transport": "stdio", "command": "echo"},
            ],
        }
        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)
        assert "mcp" in parsed["dependencies"]
        assert len(parsed["dependencies"]["mcp"]) == 1
        assert parsed["dependencies"]["mcp"][0]["name"] == "my-srv"

    def test_mcp_deps_with_apm_deps(self):
        """Both apm and mcp deps coexist."""
        manifest = {
            "name": "both-plugin",
            "dependencies": {"dep-a": "^1.0"},
            "_mcp_deps": [
                {"name": "srv", "registry": False, "transport": "http", "url": "https://x"},
            ],
        }
        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)
        assert "apm" in parsed["dependencies"]
        assert "mcp" in parsed["dependencies"]

    def test_no_mcp_deps_no_section(self):
        """No _mcp_deps → no mcp key in dependencies."""
        manifest = {"name": "no-mcp"}
        yml_str = _generate_apm_yml(manifest)
        parsed = yaml.safe_load(yml_str)
        assert "dependencies" not in parsed


# ------------------------------------------------------------------
# Issue #1666 -- synthesize_apm_yml_from_plugin must preserve existing
# apm.yml resolution-critical blocks when normalising a dual-format
# package (plugin.json + apm.yml).
# ------------------------------------------------------------------
