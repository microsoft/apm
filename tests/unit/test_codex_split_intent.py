"""A Codex compatibility manifest is split-installed like any plugin (#3920)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from apm_cli.models.validation import PackageType, detect_package_type, validate_apm_package

pytestmark = pytest.mark.component


def _plugin(root):
    manifest = root / ".codex-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"name": "example", "version": "1.0.0", "mcpServers": "./mcp.codex.json"})
    )
    (root / "mcp.codex.json").write_text(
        json.dumps({"mcpServers": {"example": {"command": "DO_NOT_EXECUTE"}}})
    )
    skill = root / "skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: example\ndescription: inert\n---\nExample\n")
    return manifest


def test_codex_only_manifest_normalizes_into_loose_primitives(tmp_path):
    manifest = _plugin(tmp_path)
    original = manifest.read_bytes()
    kind, selected = detect_package_type(tmp_path)
    assert kind is PackageType.MARKETPLACE_PLUGIN
    assert selected == manifest
    result = validate_apm_package(tmp_path)
    assert result.is_valid, result.errors
    assert result.package_type is PackageType.MARKETPLACE_PLUGIN
    assert result.package.name == "example"
    assert [dep.name for dep in result.package.get_mcp_dependencies()] == ["example"]
    assert (tmp_path / "apm.yml").is_file()
    assert (tmp_path / ".apm" / "skills" / "example" / "SKILL.md").is_file()
    assert manifest.read_bytes() == original


def test_read_only_validation_refuses_codex_plugin_like_any_plugin(tmp_path):
    _plugin(tmp_path)
    result = validate_apm_package(tmp_path, read_only=True)
    assert not result.is_valid
    assert not (tmp_path / "apm.yml").exists()


def test_codex_consumer_opt_in_does_not_change_producer_manifest_lookup(tmp_path):
    """Producer lookup retains its prior contract; only consumer admission opts in."""
    from apm_cli.utils.helpers import find_plugin_json

    manifest = _plugin(tmp_path)
    assert find_plugin_json(tmp_path) is None
    assert find_plugin_json(tmp_path, include_codex=True) == manifest


@pytest.mark.parametrize("location", ["plugin.json", ".claude-plugin/plugin.json"])
def test_existing_legacy_manifest_precedes_codex_compatibility_overlay(tmp_path, location):
    _plugin(tmp_path)
    legacy = tmp_path / location
    legacy.parent.mkdir(exist_ok=True)
    legacy.write_text(json.dumps({"name": "legacy", "version": "1.0.0"}))
    kind, selected = detect_package_type(tmp_path)
    assert kind is PackageType.MARKETPLACE_PLUGIN
    assert selected == legacy


def test_eligible_apm_intent_precedes_codex_compatibility_overlay(tmp_path):
    _plugin(tmp_path)
    (tmp_path / "apm.yml").write_text(
        "name: own-package\nversion: 1.0.0\ndependencies:\n  apm:\n    - owner/dependency\n"
    )
    kind, selected = detect_package_type(tmp_path)
    assert kind is PackageType.SKILL_BUNDLE
    assert selected is None


def test_malformed_codex_manifest_does_not_fall_back_to_skills(tmp_path):
    manifest = _plugin(tmp_path)
    manifest.write_text("{malformed")
    result = validate_apm_package(tmp_path)
    assert not result.is_valid
    assert not (tmp_path / ".apm").exists()
    assert not (tmp_path / "apm.yml").exists()


def test_read_only_schema_plugin_reuses_canonical_detection(tmp_path, monkeypatch):
    from apm_cli.install.drift import _build_package_info
    from apm_cli.models.apm_package import DependencyReference
    from tests.unit.copilot_plugins._builders import write_agent_plugin

    write_agent_plugin(tmp_path, name="schema-example")
    locked = SimpleNamespace(
        version="1.0.0",
        repo_url="owner/example",
        resolved_ref="main",
        resolved_commit="abc",
        package_type="agent_plugin",
        to_dependency_ref=lambda: DependencyReference.parse("owner/example"),
    )

    def reuse_detection(*args, **kwargs):
        assert kwargs.get("agent_plugin_detection") is not None, (
            "canonical schema selection must not rerun legacy detection"
        )
        return detect_package_type(*args, **kwargs)

    monkeypatch.setattr("apm_cli.models.validation.detect_package_type", reuse_detection)
    info = _build_package_info(locked, tmp_path)
    assert info.package_type is PackageType.AGENT_PLUGIN
