"""Regression tests for the Agent Plugin compatibility package bridge."""

from __future__ import annotations

import json
from pathlib import Path

from apm_cli.agent_plugins import MCP_SCHEMA_ID, PLUGIN_SCHEMA_ID
from apm_cli.deps.package_validator import PackageValidator, stamp_plugin_version
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.validation import PackageType, validate_apm_package
from apm_cli.utils.yaml_io import dump_yaml


def _write_plugin(root: Path, *, version: str | None = "1.2.3") -> None:
    document: dict[str, object] = {
        "$schema": PLUGIN_SCHEMA_ID,
        "name": "bridge.plugin",
        "description": "Canonical plugin",
        "author": {"name": "APM Team"},
        "license": "MIT",
    }
    if version is not None:
        document["version"] = version
    (root / "plugin.json").write_text(json.dumps(document), encoding="utf-8")


def _write_skill(root: Path) -> None:
    skill = root / "skills" / "bridge-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: bridge-skill\ndescription: Bridge skill\n---\n\nUse the bridge.\n",
        encoding="utf-8",
    )


def _write_mcp(root: Path) -> None:
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "bridge": {
                        "type": "stdio",
                        "command": "./bin/bridge",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_native_validation_projects_ir_without_filesystem_mutation(tmp_path: Path) -> None:
    _write_plugin(tmp_path)
    _write_skill(tmp_path)
    _write_mcp(tmp_path)
    apm_yml = tmp_path / "apm.yml"
    dump_yaml(
        {
            "dependencies": {"apm": ["owner/repo#v1.0.0"]},
            "targets": ["copilot"],
            "type": "skill",
        },
        apm_yml,
    )
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    result = validate_apm_package(tmp_path)

    assert result.is_valid is True
    assert result.package_type == PackageType.AGENT_PLUGIN
    assert result.agent_plugin is not None
    assert result.package is not None
    assert result.package.agent_plugin is result.agent_plugin
    assert result.package.name == result.agent_plugin.identity.name == "bridge.plugin"
    assert result.package.version == result.agent_plugin.identity.version == "1.2.3"
    assert result.package.author == "APM Team"
    assert result.package.package_path == result.agent_plugin.root
    assert result.package.source_path == result.agent_plugin.root
    assert result.package.canonical_targets == ("copilot",)
    assert [dep.repo_url for dep in result.package.get_apm_dependencies()] == ["owner/repo"]
    assert tuple(skill.name for skill in result.package.agent_plugin.components.skills) == (
        "bridge-skill",
    )
    assert tuple(server.name for server in result.package.agent_plugin.components.mcp_servers) == (
        "bridge",
    )
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_native_validation_preserves_explicit_dependency_source_anchor(tmp_path: Path) -> None:
    installed = tmp_path / "apm_modules" / "owner" / "plugin"
    installed.mkdir(parents=True)
    original = tmp_path / "sources" / "plugin"
    original.mkdir(parents=True)
    _write_plugin(installed)

    result = validate_apm_package(installed, source_path=original)

    assert result.is_valid is True
    assert result.package is not None
    assert result.package.package_path == installed.resolve()
    assert result.package.source_path == original.resolve()


def test_native_projection_preserves_empty_json_container_types(tmp_path: Path) -> None:
    _write_plugin(tmp_path)
    dump_yaml({"includes": [], "scripts": {}}, tmp_path / "apm.yml")

    result = validate_apm_package(tmp_path)

    assert result.is_valid is True
    assert result.package is not None
    assert result.package.includes == []
    assert result.package.scripts == {}


def test_unversioned_native_plugin_is_not_transport_stamped(tmp_path: Path) -> None:
    _write_plugin(tmp_path, version=None)

    result = validate_apm_package(tmp_path)

    assert result.package_type == PackageType.AGENT_PLUGIN
    assert result.package is not None
    assert result.package.version == "0.0.0"
    stamp_plugin_version(
        result.package,
        result.package_type,
        "abcdef0123456789",
        tmp_path,
    )
    assert result.package.version == "0.0.0"
    assert not (tmp_path / "apm.yml").exists()


def test_invalid_apm_projection_returns_typed_agent_plugin_error(tmp_path: Path) -> None:
    _write_plugin(tmp_path)
    dump_yaml({"targets": ["not-a-runtime"]}, tmp_path / "apm.yml")

    result = validate_apm_package(tmp_path)

    assert result.is_valid is False
    assert result.package_type == PackageType.AGENT_PLUGIN
    assert result.package is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith(
        "Failed to process Agent Plugin: Invalid Agent Plugin APM configuration:"
    )


def test_package_validator_routes_native_plugin_through_projection(tmp_path: Path) -> None:
    _write_plugin(tmp_path)

    result = PackageValidator().validate_package_structure(tmp_path)

    assert result.is_valid is True
    assert result.package_type == PackageType.AGENT_PLUGIN
    assert result.agent_plugin is not None
    assert result.package is not None


def test_from_apm_yml_delegates_to_public_mapping_owner(tmp_path: Path) -> None:
    data = {
        "name": "mapping-owner",
        "version": "2.0.0",
        "dependencies": {"apm": ["owner/repo#v2.0.0"]},
        "targets": ["copilot"],
    }
    apm_yml = tmp_path / "apm.yml"
    dump_yaml(data, apm_yml)

    from_file = APMPackage.from_apm_yml(apm_yml, source_path=tmp_path)
    from_mapping = APMPackage.from_mapping(
        data,
        package_path=tmp_path,
        source_path=tmp_path,
        manifest_path=apm_yml,
    )

    assert from_file.name == from_mapping.name
    assert from_file.version == from_mapping.version
    assert from_file.canonical_targets == from_mapping.canonical_targets
    assert [dep.repo_url for dep in from_file.get_apm_dependencies()] == [
        dep.repo_url for dep in from_mapping.get_apm_dependencies()
    ]
