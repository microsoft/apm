"""Behavioral contract tests for the canonical Agent Plugin loader."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from apm_cli.agent_plugins import (
    MCP_SCHEMA_ID,
    PLUGIN_SCHEMA_ID,
    AgentPluginLegacyBoundaryError,
    AgentPluginManifestAuthorityError,
    AgentPluginManifestError,
    NotAgentPluginError,
    load_agent_plugin,
)
from apm_cli.deps.plugin_parser import normalize_plugin_directory


def _write_manifest(root: Path, **overrides: object) -> None:
    root.mkdir(parents=True, exist_ok=True)
    document: dict[str, object] = {
        "$schema": PLUGIN_SCHEMA_ID,
        "name": "contract.plugin",
        "version": "1.2.3",
        "extensions": {
            "com.microsoft.apm": {
                "schemaVersion": "1",
                "feature": {"enabled": True},
            }
        },
    }
    document.update(overrides)
    (root / "plugin.json").write_text(json.dumps(document), encoding="utf-8")


def _write_valid_skill(root: Path, name: str) -> None:
    skill_root = root / "skills" / name
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n\nUse the skill.\n",
        encoding="utf-8",
    )


def _write_mcp(root: Path, servers: dict[str, object]) -> None:
    (root / "mcp.json").write_text(
        json.dumps({"$schema": MCP_SCHEMA_ID, "mcpServers": servers}),
        encoding="utf-8",
    )


def test_loader_returns_frozen_ir_with_component_provenance(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    _write_valid_skill(tmp_path, "deploy")
    _write_mcp(
        tmp_path,
        {
            "local": {
                "type": "stdio",
                "command": "./bin/server",
                "args": ["--data", "${PLUGIN_DATA}/server"],
                "env": {"CONFIG": "${PLUGIN_ROOT}/config.json"},
                "cwd": "${PLUGIN_ROOT}",
            }
        },
    )

    plugin = load_agent_plugin(tmp_path)

    assert plugin.identity.name == "contract.plugin"
    assert tuple(skill.directory_name for skill in plugin.components.skills) == ("deploy",)
    assert tuple(server.name for server in plugin.components.mcp_servers) == ("local",)
    assert plugin.components.mcp_servers[0].provenance.json_pointer == "/mcpServers/local"
    assert plugin.apm_extension is not None
    assert plugin.apm_extension.schema_version == "1"
    with pytest.raises(FrozenInstanceError):
        plugin.identity.name = "mutated"  # type: ignore[misc]


def test_root_manifest_name_is_case_sensitive(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    (tmp_path / "plugin.json").rename(tmp_path / "Plugin.json")

    with pytest.raises(NotAgentPluginError):
        load_agent_plugin(tmp_path)


@pytest.mark.parametrize("legacy_name", [".mcp.json", "MCP.json", "nested/mcp.json"])
def test_conforming_discovery_uses_exact_root_mcp_json(
    tmp_path: Path,
    legacy_name: str,
) -> None:
    _write_manifest(tmp_path)
    legacy_path = tmp_path / legacy_name
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {"legacy": {"type": "stdio", "command": "legacy"}},
            }
        ),
        encoding="utf-8",
    )

    plugin = load_agent_plugin(tmp_path)

    assert plugin.components.mcp_servers == ()
    assert plugin.diagnostics == ()


def test_root_mcp_invalid_server_is_reported_without_dropping_siblings(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    _write_mcp(
        tmp_path,
        {
            "good": {"type": "stdio", "command": "good"},
            "bad": {"type": "stdio", "command": "../bad"},
        },
    )

    plugin = load_agent_plugin(tmp_path)

    assert tuple(server.name for server in plugin.components.mcp_servers) == ("good",)
    assert tuple(diagnostic.code for diagnostic in plugin.diagnostics) == ("mcp.server.invalid",)
    assert "bad" in plugin.diagnostics[0].message


def test_invalid_skill_directory_is_reported(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    invalid = tmp_path / "skills" / "invalid"
    invalid.mkdir(parents=True)
    (invalid / "skill.md").write_text("# wrong case\n", encoding="utf-8")

    plugin = load_agent_plugin(tmp_path)

    assert plugin.components.skills == ()
    assert tuple(diagnostic.code for diagnostic in plugin.diagnostics) == (
        "skill.manifest.missing",
    )


def test_malformed_schema_bearing_manifest_fails_closed(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugin.json").write_text(
        '{"$schema":"https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",',
        encoding="utf-8",
    )

    with pytest.raises(AgentPluginManifestError, match=r"Invalid root plugin\.json"):
        load_agent_plugin(tmp_path)
    assert not (tmp_path / "apm.yml").exists()


def test_malformed_manifest_schema_after_probe_prefix_fails_closed(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugin.json").write_text(
        '{"padding":"'
        + ("x" * 70_000)
        + '","$schema":"https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",',
        encoding="utf-8",
    )

    with pytest.raises(AgentPluginManifestError, match=r"Invalid root plugin\.json"):
        load_agent_plugin(tmp_path)


def test_oversized_root_manifest_fails_closed(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugin.json").write_text(
        '{"$schema":"'
        + PLUGIN_SCHEMA_ID
        + '","name":"native","padding":"'
        + ("x" * (5 * 1024 * 1024))
        + '"}',
        encoding="utf-8",
    )

    with pytest.raises(AgentPluginManifestError, match="exceeds"):
        load_agent_plugin(tmp_path)


def test_symlinked_root_manifest_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text(
        json.dumps({"$schema": PLUGIN_SCHEMA_ID, "name": "linked-plugin"}),
        encoding="utf-8",
    )
    (tmp_path / "plugin.json").symlink_to(target)

    with pytest.raises(AgentPluginManifestError, match="symbolic link"):
        load_agent_plugin(tmp_path)


def test_apm_yml_conflicting_identity_is_rejected(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    (tmp_path / "apm.yml").write_text(
        "name: other-plugin\nversion: 1.2.3\ndependencies:\n  apm: []\n",
        encoding="utf-8",
    )

    with pytest.raises(
        AgentPluginManifestAuthorityError,
        match=r"conflicting apm\.yml fields: name",
    ):
        load_agent_plugin(tmp_path)


def test_matching_apm_identity_is_ignored_while_configuration_is_preserved(
    tmp_path: Path,
) -> None:
    _write_manifest(tmp_path)
    (tmp_path / "apm.yml").write_text(
        "name: contract.plugin\n"
        "version: 1.2.3\n"
        "dependencies:\n"
        "  apm:\n"
        "    - owner/repo\n"
        "scripts:\n"
        "  build: python build.py\n",
        encoding="utf-8",
    )

    plugin = load_agent_plugin(tmp_path)

    assert plugin.identity.name == "contract.plugin"
    assert plugin.apm_configuration is not None
    assert tuple(key for key, _ in plugin.apm_configuration.values) == ("dependencies", "scripts")
    assert tuple(diagnostic.code for diagnostic in plugin.diagnostics) == (
        "manifest.apm_identity.ignored",
    )


def test_claude_normalizer_rejects_native_agent_plugin(tmp_path: Path) -> None:
    _write_manifest(tmp_path)

    with pytest.raises(AgentPluginLegacyBoundaryError, match="load_agent_plugin"):
        normalize_plugin_directory(tmp_path, tmp_path / "plugin.json")
    assert not (tmp_path / "apm.yml").exists()


def test_claude_normalizer_preserves_explicit_legacy_plugin_behavior(tmp_path: Path) -> None:
    (tmp_path / "plugin.json").write_text(
        json.dumps({"name": "legacy-plugin", "version": "1.0.0"}),
        encoding="utf-8",
    )

    apm_yml = normalize_plugin_directory(tmp_path, tmp_path / "plugin.json")

    assert apm_yml == tmp_path / "apm.yml"
    assert apm_yml.is_file()


def test_claude_normalizer_preserves_symlinked_schema_less_legacy_manifest(
    tmp_path: Path,
) -> None:
    target = tmp_path.parent / f"{tmp_path.name}-legacy.json"
    target.write_text(
        json.dumps({"name": "linked-legacy", "version": "1.0.0"}),
        encoding="utf-8",
    )
    manifest = tmp_path / "plugin.json"
    manifest.symlink_to(target)

    apm_yml = normalize_plugin_directory(tmp_path, manifest)

    assert apm_yml.is_file()
    assert "name: linked-legacy" in apm_yml.read_text(encoding="utf-8")


def test_claude_normalizer_preserves_oversized_schema_less_legacy_manifest(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "plugin.json"
    manifest.write_text(
        '{"name":"legacy","padding":"' + ("x" * (5 * 1024 * 1024)) + '"}',
        encoding="utf-8",
    )

    apm_yml = normalize_plugin_directory(tmp_path, manifest)

    assert apm_yml.is_file()
    assert f"name: {tmp_path.name}" in apm_yml.read_text(encoding="utf-8")


def test_generic_credential_reference_is_not_portable_auth(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    _write_mcp(
        tmp_path,
        {
            "bad": {
                "type": "stdio",
                "command": "tool",
                "env": {"API_TOKEN": "${TOKEN}"},
            }
        },
    )

    plugin = load_agent_plugin(tmp_path)

    assert plugin.components.mcp_servers == ()
    assert "no portable credential references" in plugin.diagnostics[0].message


def test_mcp_unknown_server_field_isolated_from_valid_sibling(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    _write_mcp(
        tmp_path,
        {
            "good": {"type": "stdio", "command": "good"},
            "bad": {"type": "stdio", "command": "bad", "unexpected": True},
        },
    )

    plugin = load_agent_plugin(tmp_path)

    assert tuple(server.name for server in plugin.components.mcp_servers) == ("good",)
    assert "unexpected" in plugin.diagnostics[0].message


@pytest.mark.parametrize(
    ("server", "message"),
    [
        (
            {"type": "stdio", "command": "tool", "args": ["${HOME}/tool"]},
            "only ${PLUGIN_ROOT} and ${PLUGIN_DATA} are portable",
        ),
        (
            {"type": "stdio", "command": "tool", "env": {"CONFIG": "${HOME}/config"}},
            "only ${PLUGIN_ROOT} and ${PLUGIN_DATA} are portable",
        ),
        (
            {"type": "stdio", "command": "tool", "cwd": "${PLUGIN_ROOT}/${HOME}"},
            "only ${PLUGIN_ROOT} and ${PLUGIN_DATA} are portable",
        ),
        (
            {"type": "stdio", "command": "tool", "cwd": "./${TOKEN}"},
            "only ${PLUGIN_ROOT} and ${PLUGIN_DATA} are portable",
        ),
        (
            {"type": "streamable-http", "url": "https://example.com/${TENANT}"},
            "placeholder expansion is not defined",
        ),
        (
            {
                "type": "streamable-http",
                "url": "https://example.com/mcp",
                "headers": {"X-Tenant": "${TENANT}"},
            },
            "placeholder expansion is not defined",
        ),
    ],
)
def test_non_portable_mcp_placeholders_are_rejected(
    tmp_path: Path,
    server: dict[str, object],
    message: str,
) -> None:
    _write_manifest(tmp_path)
    _write_mcp(tmp_path, {"bad": server})

    plugin = load_agent_plugin(tmp_path)

    assert plugin.components.mcp_servers == ()
    assert message in plugin.diagnostics[0].message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command", "./../outside"),
        ("cwd", "./../outside"),
        ("cwd", "${PLUGIN_ROOT}/../outside"),
        ("cwd", "${PLUGIN_DATA}/../outside"),
        ("command", "./..\\outside"),
        ("cwd", "./..\\outside"),
        ("cwd", "${PLUGIN_ROOT}/..\\outside"),
        ("cwd", "${PLUGIN_DATA}/..\\outside"),
    ],
)
def test_mcp_paths_cannot_escape_their_permitted_root(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    _write_manifest(tmp_path)
    server = {"type": "stdio", "command": "tool", field: value}
    _write_mcp(tmp_path, {"bad": server})

    plugin = load_agent_plugin(tmp_path)

    assert plugin.components.mcp_servers == ()
    assert "escape" in plugin.diagnostics[0].message


@pytest.mark.parametrize("field", ["command", "cwd"])
def test_mcp_plugin_relative_paths_cannot_resolve_through_escaping_symlink(
    tmp_path: Path,
    field: str,
) -> None:
    _write_manifest(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    server = {"type": "stdio", "command": "tool", field: "./linked/server"}
    _write_mcp(tmp_path, {"bad": server})

    plugin = load_agent_plugin(tmp_path)

    assert plugin.components.mcp_servers == ()
    assert "escape" in plugin.diagnostics[0].message


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x1f", "\r", "\n", "\x7f"])
def test_mcp_http_header_values_reject_prohibited_controls(
    tmp_path: Path,
    control: str,
) -> None:
    _write_manifest(tmp_path)
    _write_mcp(
        tmp_path,
        {
            "bad": {
                "type": "streamable-http",
                "url": "https://example.com/mcp",
                "headers": {"X-Value": f"before{control}after"},
            }
        },
    )

    plugin = load_agent_plugin(tmp_path)

    assert plugin.components.mcp_servers == ()
    assert "HTTP field-value control character" in plugin.diagnostics[0].message


def test_mcp_http_header_values_allow_horizontal_tab(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    _write_mcp(
        tmp_path,
        {
            "valid": {
                "type": "streamable-http",
                "url": "https://example.com/mcp",
                "headers": {"X-Value": "before\tafter"},
            }
        },
    )

    plugin = load_agent_plugin(tmp_path)

    assert tuple(server.name for server in plugin.components.mcp_servers) == ("valid",)
