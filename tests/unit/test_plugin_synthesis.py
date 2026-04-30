"""Unit tests for synthesize_plugin_json_from_apm_yml.

Focused test suite for the plugin.json synthesis from apm.yml identity fields.
"""

from pathlib import Path

import pytest
import yaml

from apm_cli.deps.plugin_parser import synthesize_plugin_json_from_apm_yml


def _write_apm_yml(tmp_path: Path, data: dict) -> Path:
    """Write an apm.yml file and return its path."""
    path = tmp_path / "apm.yml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


class TestPluginJsonSynthesis:
    """Tests for synthesize_plugin_json_from_apm_yml."""

    def test_basic_synthesis(self, tmp_path):
        """Synthesizes plugin.json with mapped fields."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "my-plugin",
                "version": "1.0.0",
                "description": "A cool plugin",
                "author": "Jane Doe",
                "license": "MIT",
            },
        )

        result = synthesize_plugin_json_from_apm_yml(yml)

        assert result["name"] == "my-plugin"
        assert result["version"] == "1.0.0"
        assert result["description"] == "A cool plugin"
        assert result["author"] == {"name": "Jane Doe"}
        assert result["license"] == "MIT"

    def test_author_string_to_object(self, tmp_path):
        """Author string maps to {name: string} object."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test",
                "version": "1.0.0",
                "author": "John Smith",
            },
        )

        result = synthesize_plugin_json_from_apm_yml(yml)

        assert result["author"] == {"name": "John Smith"}
        assert isinstance(result["author"], dict)

    def test_author_numeric_coerced_to_string(self, tmp_path):
        """Numeric author values are coerced to strings."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test",
                "version": "1.0.0",
                "author": 42,
            },
        )

        result = synthesize_plugin_json_from_apm_yml(yml)

        assert result["author"] == {"name": "42"}

    def test_missing_name_raises(self, tmp_path):
        """Missing name in apm.yml raises ValueError."""
        yml = _write_apm_yml(tmp_path, {"version": "1.0.0"})

        with pytest.raises(ValueError, match="name"):
            synthesize_plugin_json_from_apm_yml(yml)

    def test_empty_name_raises(self, tmp_path):
        """Empty string name raises ValueError."""
        yml = _write_apm_yml(tmp_path, {"name": "", "version": "1.0.0"})

        with pytest.raises(ValueError, match="name"):
            synthesize_plugin_json_from_apm_yml(yml)

    def test_optional_fields_omitted_if_missing(self, tmp_path):
        """Optional fields (description, license, author) not in output if missing from apm.yml."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "minimal-pkg",
                "version": "1.0.0",
            },
        )

        result = synthesize_plugin_json_from_apm_yml(yml)

        assert result["name"] == "minimal-pkg"
        assert result["version"] == "1.0.0"
        assert "description" not in result
        assert "author" not in result
        assert "license" not in result

    def test_version_omitted_if_missing(self, tmp_path):
        """Version is optional in output when absent from apm.yml."""
        yml = _write_apm_yml(tmp_path, {"name": "no-version"})

        result = synthesize_plugin_json_from_apm_yml(yml)

        assert result["name"] == "no-version"
        assert "version" not in result

    def test_file_not_found_raises(self, tmp_path):
        """Non-existent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            synthesize_plugin_json_from_apm_yml(tmp_path / "nonexistent.yml")

    def test_invalid_yaml_raises(self, tmp_path):
        """Invalid YAML raises ValueError."""
        bad = tmp_path / "apm.yml"
        bad.write_text("{{invalid: yaml: [", encoding="utf-8")

        with pytest.raises(ValueError, match="Invalid YAML"):
            synthesize_plugin_json_from_apm_yml(bad)

    def test_non_dict_yaml_raises(self, tmp_path):
        """YAML that is a list instead of dict raises ValueError."""
        bad = tmp_path / "apm.yml"
        bad.write_text("- item1\n- item2\n", encoding="utf-8")

        with pytest.raises(ValueError, match="name"):
            synthesize_plugin_json_from_apm_yml(bad)

    def test_license_without_author(self, tmp_path):
        """License can be present without author."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test",
                "version": "1.0.0",
                "license": "Apache-2.0",
            },
        )

        result = synthesize_plugin_json_from_apm_yml(yml)

        assert result["license"] == "Apache-2.0"
        assert "author" not in result

    def test_all_fields_present(self, tmp_path):
        """All supported fields are mapped correctly."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "full-pkg",
                "version": "2.1.0",
                "description": "Full package",
                "author": "Acme Corp",
                "license": "ISC",
            },
        )

        result = synthesize_plugin_json_from_apm_yml(yml)

        assert set(result.keys()) == {"name", "version", "description", "author", "license"}

    def test_extra_apm_fields_ignored(self, tmp_path):
        """Fields not part of plugin spec (dependencies, scripts) are not in output."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "name": "test",
                "version": "1.0.0",
                "dependencies": {"apm": ["owner/repo"]},
                "scripts": {"build": "echo hi"},
                "target": "vscode",
            },
        )

        result = synthesize_plugin_json_from_apm_yml(yml)

        assert "dependencies" not in result
        assert "scripts" not in result
        assert "target" not in result
