"""Behavioral contract tests for the canonical Agent Plugin loader."""

from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from apm_cli.agent_plugins import (
    MCP_SCHEMA_ID,
    PLUGIN_SCHEMA_ID,
    AgentPluginLegacyBoundaryError,
    AgentPluginManifestAuthorityError,
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


def _assert_present_manifest_cannot_reach_claude(tmp_path: Path, message: str) -> None:
    with pytest.raises(AgentPluginLegacyBoundaryError, match=message):
        normalize_plugin_directory(tmp_path, tmp_path / "plugin.json")
    assert not (tmp_path / "apm.yml").exists()


@pytest.mark.parametrize("target_kind", ["inside", "outside", "dangling"])
def test_symlinked_root_manifest_cannot_reach_claude(
    tmp_path: Path,
    target_kind: str,
) -> None:
    if target_kind == "inside":
        target = tmp_path / "target.json"
    else:
        target = tmp_path.parent / f"{tmp_path.name}-{target_kind}.json"
    if target_kind != "dangling":
        target.write_text(json.dumps({"name": "legacy-or-native"}), encoding="utf-8")
    (tmp_path / "plugin.json").symlink_to(target)

    _assert_present_manifest_cannot_reach_claude(tmp_path, "symlink")


@pytest.mark.parametrize(
    "prefix",
    [
        '{"name":"legacy","padding":"',
        f'{{"$schema":"{PLUGIN_SCHEMA_ID}","padding":"',
        '{"padding":"',
    ],
)
def test_oversized_root_manifest_cannot_reach_claude(
    tmp_path: Path,
    prefix: str,
) -> None:
    suffix = f'","$schema":"{PLUGIN_SCHEMA_ID}"}}' if prefix == '{"padding":"' else '"}'
    (tmp_path / "plugin.json").write_text(
        prefix + ("x" * (5 * 1024 * 1024)) + suffix,
        encoding="utf-8",
    )

    _assert_present_manifest_cannot_reach_claude(tmp_path, "exceeds")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"name":"malformed"', "Invalid JSON"),
        (
            f'{{"$schema":"{PLUGIN_SCHEMA_ID}","$schema":"{PLUGIN_SCHEMA_ID}","name":"duplicate"}}',
            r"duplicate \$schema",
        ),
        ('{"$schema":1,"name":"non-string"}', r"\$schema must be a string"),
        ('["not-an-object"]', "JSON object"),
    ],
)
def test_invalid_present_manifest_cannot_reach_claude(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    (tmp_path / "plugin.json").write_text(content, encoding="utf-8")

    _assert_present_manifest_cannot_reach_claude(tmp_path, message)


def test_non_regular_present_manifest_cannot_reach_claude(tmp_path: Path) -> None:
    (tmp_path / "plugin.json").mkdir()

    _assert_present_manifest_cannot_reach_claude(tmp_path, "regular file")


def test_unreadable_present_manifest_cannot_reach_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_manifest(tmp_path)

    def unreadable(_path: Path, *, reject_duplicate_schema: bool = False) -> object:
        del reject_duplicate_schema
        raise OSError("permission denied")

    monkeypatch.setattr("apm_cli.agent_plugins.loader.read_json_document", unreadable)

    _assert_present_manifest_cannot_reach_claude(tmp_path, "permission denied")


def test_unreadable_manifest_directory_cannot_fall_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_iterdir = Path.iterdir

    def unreadable(path: Path):
        if path == tmp_path:
            raise OSError("directory permission denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", unreadable)

    _assert_present_manifest_cannot_reach_claude(tmp_path, "could not be determined")


def test_manifest_swap_to_symlink_is_rejected_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "plugin.json"
    manifest.write_text('{"name":"legacy"}', encoding="utf-8")
    moved = tmp_path / "moved.json"
    real_open = os.open

    def swap_then_open(path: Path, flags: int) -> int:
        manifest.rename(moved)
        manifest.symlink_to(moved)
        return real_open(path, flags)

    monkeypatch.setattr("apm_cli.agent_plugins.io.os.O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr("apm_cli.agent_plugins.io.os.open", swap_then_open)

    _assert_present_manifest_cannot_reach_claude(tmp_path, "changed during validation")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO files are unavailable")
def test_manifest_swap_to_fifo_cannot_block_or_fall_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "plugin.json"
    manifest.write_text('{"name":"legacy"}', encoding="utf-8")
    moved = tmp_path / "moved.json"
    real_open = os.open

    def swap_then_open(path: Path, flags: int) -> int:
        manifest.rename(moved)
        os.mkfifo(manifest)
        return real_open(path, flags)

    monkeypatch.setattr("apm_cli.agent_plugins.io.os.open", swap_then_open)

    _assert_present_manifest_cannot_reach_claude(tmp_path, "regular file")


def test_root_legacy_manifest_is_parsed_once_for_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "plugin.json"
    manifest.write_text('{"name":"legacy"}', encoding="utf-8")
    from apm_cli.agent_plugins import loader

    real_read = loader.read_json_document
    reads = 0

    def count_read(path: Path, *, reject_duplicate_schema: bool = False) -> object:
        nonlocal reads
        reads += 1
        return real_read(path, reject_duplicate_schema=reject_duplicate_schema)

    monkeypatch.setattr(loader, "read_json_document", count_read)

    normalize_plugin_directory(tmp_path, manifest)

    assert reads == 1


def test_nested_legacy_manifest_normalization_remains_supported(tmp_path: Path) -> None:
    manifest = tmp_path / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text('{"name":"nested-legacy"}', encoding="utf-8")

    apm_yml = normalize_plugin_directory(tmp_path, manifest)

    assert "name: nested-legacy" in apm_yml.read_text(encoding="utf-8")


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
