"""Tests for map-form outputs parsing in yml_schema (phase-3a, T-3a-09..26).

Covers:
- Map form basic parsing (single format, multiple)
- Map form with explicit path
- Map form path validation (traversal rejection)
- Back-compat list form still works + emits deprecation warning
- MarketplaceOutputSpec fields
- Sibling conflict detection
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from apm_cli.marketplace.yml_schema import (
    MarketplaceOutputSpec,
    MarketplaceYmlError,
    load_marketplace_from_apm_yml,
)


def _write_apm_yml(tmp_path: Path, marketplace_block: dict[str, Any]) -> Path:
    """Write a minimal apm.yml with the given marketplace block."""
    import yaml

    content = {
        "name": "test-pkg",
        "description": "Test package",
        "version": "1.0.0",
        "marketplace": {
            "owner": {"name": "Test Owner"},
            "packages": [
                {
                    "name": "my-tool",
                    "source": "org/repo",
                    "version": "1.0.0",
                    "description": "desc",
                    "category": "tools",
                }
            ],
            **marketplace_block,
        },
    }
    yml_path = tmp_path / "apm.yml"
    yml_path.write_text(yaml.dump(content, sort_keys=False), encoding="utf-8")
    return yml_path


class TestMapFormParsing:
    """T-3a-09..14: outputs as a dict (map form)."""

    def test_single_format_null_value(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {"outputs": {"claude": None}})
        config = load_marketplace_from_apm_yml(yml)
        assert config.outputs == ("claude",)
        assert len(config.output_specs) == 1
        spec = config.output_specs[0]
        assert spec.name == "claude"
        assert spec.path == ".claude-plugin/marketplace.json"
        assert spec.path_explicit is False

    def test_single_format_empty_dict(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {"outputs": {"claude": {}}})
        config = load_marketplace_from_apm_yml(yml)
        assert config.outputs == ("claude",)
        assert config.output_specs[0].path_explicit is False

    def test_multiple_formats(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {"outputs": {"claude": {}, "codex": {}}})
        config = load_marketplace_from_apm_yml(yml)
        assert set(config.outputs) == {"claude", "codex"}
        assert len(config.output_specs) == 2

    def test_explicit_path(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {"outputs": {"claude": {"path": "custom/output.json"}}})
        config = load_marketplace_from_apm_yml(yml)
        spec = config.output_specs[0]
        assert spec.path == "custom/output.json"
        assert spec.path_explicit is True

    def test_unknown_format_raises(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {"outputs": {"unknown_format": {}}})
        with pytest.raises(MarketplaceYmlError, match="Unknown marketplace output"):
            load_marketplace_from_apm_yml(yml)

    def test_empty_map_raises(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {"outputs": {}})
        with pytest.raises(MarketplaceYmlError, match="at least one"):
            load_marketplace_from_apm_yml(yml)

    def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {"outputs": {"claude": {"path": "../escape/file.json"}}})
        with pytest.raises(MarketplaceYmlError, match="path"):
            load_marketplace_from_apm_yml(yml)

    def test_unknown_key_in_format_entry(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {"outputs": {"claude": {"path": "x.json", "bogus": True}}})
        with pytest.raises(MarketplaceYmlError, match="Unknown key"):
            load_marketplace_from_apm_yml(yml)

    def test_non_dict_format_value_raises(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {"outputs": {"claude": "not_a_dict"}})
        with pytest.raises(MarketplaceYmlError, match="mapping or null"):
            load_marketplace_from_apm_yml(yml)

    def test_no_deprecation_warning_for_map(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {"outputs": {"claude": {}}})
        config = load_marketplace_from_apm_yml(yml)
        assert not any("deprecated" in w for w in config.warnings)


class TestListFormBackCompat:
    """T-3a-15..18: list form back-compat."""

    def test_list_form_still_works(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {"outputs": ["claude"]})
        config = load_marketplace_from_apm_yml(yml)
        assert config.outputs == ("claude",)
        assert len(config.output_specs) == 1
        assert config.output_specs[0].name == "claude"

    def test_list_form_emits_deprecation_warning(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {"outputs": ["claude"]})
        config = load_marketplace_from_apm_yml(yml)
        assert any("deprecated" in w for w in config.warnings)
        assert any("map form" in w for w in config.warnings)

    def test_string_form_still_works(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {"outputs": "claude"})
        config = load_marketplace_from_apm_yml(yml)
        assert config.outputs == ("claude",)
        assert any("deprecated" in w for w in config.warnings)

    def test_none_outputs_defaults_to_claude(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(tmp_path, {})
        config = load_marketplace_from_apm_yml(yml)
        assert config.outputs == ("claude",)
        assert config.output_specs[0].name == "claude"
        # No deprecation warning for the default
        assert not any("deprecated" in w for w in config.warnings)


class TestOutputSpecFields:
    """T-3a-19..22: MarketplaceOutputSpec dataclass."""

    def test_dataclass_frozen(self) -> None:
        spec = MarketplaceOutputSpec(name="claude", path="x.json")
        with pytest.raises((TypeError, AttributeError)):
            spec.name = "other"  # type: ignore[misc]

    def test_defaults(self) -> None:
        spec = MarketplaceOutputSpec(name="claude", path="x.json")
        assert spec.path_explicit is False

    def test_explicit_path_flag(self) -> None:
        spec = MarketplaceOutputSpec(name="claude", path="x.json", path_explicit=True)
        assert spec.path_explicit is True


class TestSiblingConflict:
    """T-3a-23..26: sibling block vs outputs map conflict."""

    def test_sibling_wins_on_conflict(self, tmp_path: Path) -> None:
        """When outputs.claude.path and marketplace.claude.output differ,
        sibling (marketplace.claude.output) wins."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "outputs": {"claude": {"path": "map-path.json"}},
                "claude": {"output": "sibling-path.json"},
            },
        )
        config = load_marketplace_from_apm_yml(yml)
        # Sibling wins
        spec = next(s for s in config.output_specs if s.name == "claude")
        assert spec.path == "sibling-path.json"

    def test_sibling_conflict_emits_warning(self, tmp_path: Path) -> None:
        yml = _write_apm_yml(
            tmp_path,
            {
                "outputs": {"claude": {"path": "map-path.json"}},
                "claude": {"output": "sibling-path.json"},
            },
        )
        config = load_marketplace_from_apm_yml(yml)
        assert any("conflicts" in w for w in config.warnings)

    def test_no_conflict_when_paths_match(self, tmp_path: Path) -> None:
        """No warning when both sources agree on the same path."""
        yml = _write_apm_yml(
            tmp_path,
            {
                "outputs": {"claude": {"path": "same.json"}},
                "claude": {"output": "same.json"},
            },
        )
        config = load_marketplace_from_apm_yml(yml)
        assert not any("conflicts" in w for w in config.warnings)
