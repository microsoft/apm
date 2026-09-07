"""Behavioral contract tests for the canonical Agent Plugin loader."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest

from apm_cli.agent_plugins import (
    MCP_SCHEMA_ID,
    PLUGIN_SCHEMA_ID,
    AgentPluginError,
    AgentPluginLegacyBoundaryError,
    AgentPluginManifestAuthorityError,
    AssetInventoryError,
    FrozenJsonArray,
    FrozenJsonObject,
    FrozenJsonValue,
    NotAgentPluginError,
    detect_agent_plugin,
    load_agent_plugin,
    open_verified_asset,
    thaw_frozen_json,
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
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "server").write_text("server", encoding="utf-8")
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
    executable = plugin.components.mcp_servers[0].executables[0]
    assert executable.plugin_relative_path == "bin/server"
    assert executable.asset is not None
    assert executable.asset.sha256 == hashlib.sha256(b"server").hexdigest()
    assert plugin.apm_extension is not None
    assert plugin.apm_extension.schema_version == "1"
    with pytest.raises(FrozenInstanceError):
        plugin.identity.name = "mutated"  # type: ignore[misc]


def test_asset_digest_verification_rejects_post_load_mutation(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    _write_valid_skill(tmp_path, "digest")
    plugin = load_agent_plugin(tmp_path)
    asset = plugin.components.skills[0].assets[0]

    with open_verified_asset(tmp_path, asset) as handle:
        assert handle.read() == (tmp_path / asset.path).read_bytes()
    (tmp_path / asset.path).write_text("changed", encoding="utf-8")

    with pytest.raises(AssetInventoryError, match="no longer matches"):
        with open_verified_asset(tmp_path, asset):
            pass


@pytest.mark.windows_compat
def test_inventory_accepts_portable_descriptor_mode_difference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path and descriptor mode emulation must not reject an unchanged asset."""
    _write_manifest(tmp_path)
    _write_valid_skill(tmp_path, "portable")
    real_fstat = os.fstat

    def fstat_with_different_permission_bits(descriptor: int) -> SimpleNamespace:
        result = real_fstat(descriptor)
        return SimpleNamespace(
            st_mode=result.st_mode ^ stat.S_IWUSR,
            st_ino=result.st_ino,
            st_dev=result.st_dev,
            st_size=result.st_size,
        )

    monkeypatch.setattr(
        "apm_cli.agent_plugins.assets.os.fstat",
        fstat_with_different_permission_bits,
    )

    plugin = load_agent_plugin(tmp_path)

    assert tuple(skill.directory_name for skill in plugin.components.skills) == ("portable",)
    assert not any(diagnostic.code == "skill.assets.invalid" for diagnostic in plugin.diagnostics)
    asset = plugin.components.skills[0].assets[0]
    assert asset.sha256 == hashlib.sha256((tmp_path / asset.path).read_bytes()).hexdigest()
    with open_verified_asset(tmp_path, asset) as handle:
        assert handle.read() == (tmp_path / asset.path).read_bytes()


@pytest.mark.windows_compat
def test_inventory_rejects_path_replacement_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed pathname must not be mistaken for the descriptor inventory."""
    from apm_cli.agent_plugins.assets import AssetInventory

    asset_path = tmp_path / "asset.txt"
    asset_path.write_text("original", encoding="utf-8")
    moved = tmp_path / "original.txt"
    real_open = os.open

    def replace_then_open(path: Path, flags: int) -> int:
        asset_path.rename(moved)
        asset_path.write_text("replacement", encoding="utf-8")
        return real_open(path, flags)

    monkeypatch.setattr("apm_cli.agent_plugins.assets.os.open", replace_then_open)

    with pytest.raises(AssetInventoryError, match="changed during inventory"):
        AssetInventory(tmp_path).collect_file(asset_path)


@pytest.mark.windows_compat
def test_verified_asset_descriptor_is_stable_after_path_replacement(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    _write_valid_skill(tmp_path, "stable")
    plugin = load_agent_plugin(tmp_path)
    asset = plugin.components.skills[0].assets[0]
    asset_path = tmp_path / asset.path
    moved = asset_path.with_name("original.md")

    with open_verified_asset(tmp_path, asset) as handle:
        if sys.platform == "win32":
            with pytest.raises(PermissionError) as exc_info:
                asset_path.rename(moved)
            assert exc_info.value.winerror == 32
            assert hashlib.sha256(handle.read()).hexdigest() == asset.sha256
        else:
            asset_path.rename(moved)
            asset_path.write_text("replacement", encoding="utf-8")
            assert hashlib.sha256(handle.read()).hexdigest() == asset.sha256

    if sys.platform == "win32":
        asset_path.rename(moved)
        asset_path.write_text("replacement", encoding="utf-8")
        assert moved.read_text(encoding="utf-8") != asset_path.read_text(encoding="utf-8")


def test_collect_file_rejects_directory_instead_of_selecting_child(tmp_path: Path) -> None:
    from apm_cli.agent_plugins.assets import AssetInventory

    directory = tmp_path / "bin"
    directory.mkdir()
    (directory / "child").write_text("child", encoding="utf-8")

    with pytest.raises(AssetInventoryError, match="regular file"):
        AssetInventory(tmp_path).collect_file(directory)


def test_unreferenced_bin_is_not_promoted_but_mcp_reference_is_inventoried(
    tmp_path: Path,
) -> None:
    _write_manifest(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "used").write_text("used", encoding="utf-8")
    (bin_dir / "unused").write_text("unused", encoding="utf-8")
    _write_mcp(
        tmp_path,
        {"tool": {"type": "stdio", "command": "./bin/used"}},
    )

    plugin = load_agent_plugin(tmp_path)

    server = plugin.components.mcp_servers[0]
    assert tuple(
        executable.asset.path for executable in server.executables if executable.asset is not None
    ) == ("bin/used",)
    all_paths = {asset.path for skill in plugin.components.skills for asset in skill.assets} | {
        executable.asset.path for executable in server.executables if executable.asset is not None
    }
    assert "bin/unused" not in all_paths


def test_symlinked_skill_asset_is_rejected_without_aborting_mcp(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    _write_valid_skill(tmp_path, "unsafe")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "skills" / "unsafe" / "linked.txt").symlink_to(outside)
    _write_mcp(tmp_path, {"safe": {"type": "stdio", "command": "safe"}})

    plugin = load_agent_plugin(tmp_path)

    assert plugin.components.skills == ()
    assert tuple(server.name for server in plugin.components.mcp_servers) == ("safe",)
    assert any(diagnostic.code == "skill.assets.invalid" for diagnostic in plugin.diagnostics)


def test_asset_budget_violation_rejects_only_affected_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_manifest(tmp_path)
    _write_valid_skill(tmp_path, "oversized-inventory")
    _write_mcp(tmp_path, {"safe": {"type": "stdio", "command": "safe"}})
    monkeypatch.setattr("apm_cli.agent_plugins.assets.MAX_COMPONENT_ASSET_ENTRIES", 5)

    plugin = load_agent_plugin(tmp_path)

    assert plugin.components.skills == ()
    assert tuple(server.name for server in plugin.components.mcp_servers) == ("safe",)
    assert any(
        diagnostic.code == "skill.assets.invalid" and "entry package budget" in diagnostic.message
        for diagnostic in plugin.diagnostics
    )


def test_many_failing_components_consume_one_irreversible_work_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apm_cli.agent_plugins.assets import AssetInventory

    inventory = AssetInventory(tmp_path)
    attempts = 12
    hashed = 0
    original_inventory = AssetInventory._inventory_regular_file

    def count_hashes(self, *args, **kwargs):
        nonlocal hashed
        hashed += 1
        result = original_inventory(self, *args, **kwargs)
        if args[0].name == "z.txt":
            raise AssetInventoryError("adversarial late component failure")
        return result

    monkeypatch.setattr("apm_cli.agent_plugins.assets.MAX_COMPONENT_ASSET_ENTRIES", 7)
    monkeypatch.setattr(AssetInventory, "_inventory_regular_file", count_hashes)
    for index in range(attempts):
        component = tmp_path / f"component-{index:02d}"
        component.mkdir()
        (component / "a.txt").write_text("accepted", encoding="utf-8")
        (component / "z.txt").write_text("late failure", encoding="utf-8")
        with pytest.raises(AssetInventoryError):
            inventory.collect_component(component)

    assert hashed == 4
    assert inventory._entry_count == attempts + 5


def test_candidate_enumeration_stops_before_materializing_unbounded_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apm_cli.agent_plugins.assets import AssetInventory

    candidates = tmp_path / "skills"
    candidates.mkdir()
    for index in range(20):
        (candidates / f"skill-{index:02d}").mkdir()
    yielded = 0
    original_iterdir = Path.iterdir

    def count_candidates(path):
        nonlocal yielded
        for entry in original_iterdir(path):
            if path == candidates:
                yielded += 1
            yield entry

    monkeypatch.setattr("apm_cli.agent_plugins.assets.MAX_COMPONENT_ASSET_ENTRIES", 5)
    monkeypatch.setattr(Path, "iterdir", count_candidates)

    with pytest.raises(AssetInventoryError, match="entry package budget"):
        AssetInventory(tmp_path).list_component_candidates(candidates)

    assert yielded == 6


def test_repeated_asset_reference_skips_case_walk_and_work_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apm_cli.agent_plugins.assets import AssetInventory

    executable = tmp_path / "bin" / "tool"
    executable.parent.mkdir()
    executable.write_text("tool", encoding="utf-8")
    inventory = AssetInventory(tmp_path)
    case_walks = 0
    original_case_walk = AssetInventory._assert_case_unambiguous

    def count_case_walks(self, path):
        nonlocal case_walks
        case_walks += 1
        return original_case_walk(self, path)

    monkeypatch.setattr(AssetInventory, "_assert_case_unambiguous", count_case_walks)
    assets = tuple(inventory.collect_file(executable) for _ in range(1_000))

    assert len({id(asset) for asset in assets}) == 1
    assert inventory._entry_count == 1
    assert case_walks == 1


def test_repeated_read_file_does_not_double_reserve_asset_budget(tmp_path: Path) -> None:
    from apm_cli.agent_plugins.assets import AssetInventory

    declaration = tmp_path / "hooks.json"
    declaration.write_bytes(b'{"hooks":{}}')
    inventory = AssetInventory(tmp_path)

    first, first_payload = inventory.read_file(declaration, max_bytes=1_024)
    entry_count = inventory._entry_count
    byte_count = inventory._byte_count
    second, second_payload = inventory.read_file(declaration, max_bytes=1_024)

    assert second is first
    assert second_payload == first_payload
    assert inventory._entry_count == entry_count
    assert inventory._byte_count == byte_count


def test_inventory_resolves_plugin_root_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apm_cli.agent_plugins.assets import AssetInventory

    executable = tmp_path / "bin" / "tool"
    executable.parent.mkdir()
    executable.write_text("tool", encoding="utf-8")
    root_resolves = 0
    original_resolve = Path.resolve

    def count_resolves(path, *args, **kwargs):
        nonlocal root_resolves
        if path == tmp_path:
            root_resolves += 1
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", count_resolves)
    inventory = AssetInventory(tmp_path)
    for _ in range(100):
        inventory.collect_file(executable)

    assert root_resolves == 1


def test_root_non_portable_component_directories_are_ignored(tmp_path: Path) -> None:
    _write_manifest(tmp_path, extensions={})
    raw = tmp_path / "agents"
    raw.mkdir()
    (raw / "raw.md").write_text("raw", encoding="utf-8")

    plugin = load_agent_plugin(tmp_path)

    codes = {diagnostic.code for diagnostic in plugin.diagnostics}
    assert "portable.component.ignored" in codes


def test_invalid_skill_and_mcp_are_isolated_from_each_other(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    _write_valid_skill(tmp_path, "survivor")
    _write_mcp(tmp_path, {"survivor": {"type": "stdio", "command": "survivor"}})

    plugin = load_agent_plugin(tmp_path)

    assert tuple(skill.name for skill in plugin.components.skills) == ("survivor",)
    assert tuple(server.name for server in plugin.components.mcp_servers) == ("survivor",)


@pytest.mark.parametrize(
    "escape",
    [
        "../evil.sh",
        "../../evil.sh",
        "${PLUGIN_ROOT}/../evil.sh",
        r"${PLUGIN_ROOT}\..\evil.sh",
    ],
)
def test_executable_parent_escapes_are_rejected_without_decoy_matching(
    tmp_path: Path,
    escape: str,
) -> None:
    _write_manifest(tmp_path)
    (tmp_path / "evil.sh").write_text("decoy", encoding="utf-8")
    _write_mcp(
        tmp_path,
        {"unsafe": {"type": "stdio", "command": "bash", "args": [escape]}},
    )

    plugin = load_agent_plugin(tmp_path)
    diagnostic = next(
        item for item in plugin.diagnostics if item.code == "mcp.server.executable.invalid"
    )

    assert "segment '..' is a traversal sequence" in diagnostic.message
    assert plugin.components.mcp_servers == ()


def test_missing_referenced_executable_has_typed_fact_and_diagnostic(
    tmp_path: Path,
) -> None:
    _write_manifest(tmp_path)
    _write_mcp(tmp_path, {"missing": {"type": "stdio", "command": "./bin/missing"}})

    plugin = load_agent_plugin(tmp_path)

    assert any(item.code == "mcp.server.executable.missing" for item in plugin.diagnostics)
    executable = plugin.components.mcp_servers[0].executables[0]
    assert executable.plugin_relative_path is not None
    assert executable.asset is None


def test_component_root_nfc_case_ambiguity_has_surface_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_manifest(tmp_path)
    _write_valid_skill(tmp_path, "valid")
    directory = tmp_path
    alias = tmp_path / "Skills"
    expected_code = "skills.location.ambiguous"
    original_iterdir = Path.iterdir

    def ambiguous_iterdir(path):
        entries = list(original_iterdir(path))
        if path == directory:
            entries.append(alias)
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", ambiguous_iterdir)
    plugin = load_agent_plugin(tmp_path)

    assert any(item.code == expected_code for item in plugin.diagnostics)


def test_nested_asset_nfc_ambiguity_rejects_only_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_manifest(tmp_path)
    _write_valid_skill(tmp_path, "ambiguous")
    component_root = tmp_path / "skills" / "ambiguous"
    expected_code = "skill.assets.invalid"
    canonical = component_root / "\u00e9.txt"
    alias = component_root / "e\u0301.txt"
    canonical.write_text("content", encoding="utf-8")
    original_iterdir = Path.iterdir
    original_lstat = Path.lstat

    def ambiguous_iterdir(path):
        entries = list(original_iterdir(path))
        if path == component_root:
            entries.append(alias)
        return iter(entries)

    def alias_lstat(path):
        if path == alias:
            return original_lstat(canonical)
        return original_lstat(path)

    monkeypatch.setattr(Path, "iterdir", ambiguous_iterdir)
    monkeypatch.setattr(Path, "lstat", alias_lstat)
    plugin = load_agent_plugin(tmp_path)

    assert any(item.code == expected_code for item in plugin.diagnostics)


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


def test_apm_configuration_preserves_empty_container_types(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    expected = {
        "dependencies": {
            "apm": [
                {},
                {
                    "emptyObject": {},
                    "emptyArray": [],
                    "nested": [{}, [], {"array": [[], {}]}],
                },
            ]
        },
        "policy": {
            "emptyObject": {},
            "emptyArray": [],
            "mixed": {"array": [{}, []]},
        },
    }
    (tmp_path / "apm.yml").write_text(
        "dependencies:\n"
        "  apm:\n"
        "    - {}\n"
        "    - emptyObject: {}\n"
        "      emptyArray: []\n"
        "      nested:\n"
        "        - {}\n"
        "        - []\n"
        "        - array:\n"
        "            - []\n"
        "            - {}\n"
        "policy:\n"
        "  emptyObject: {}\n"
        "  emptyArray: []\n"
        "  mixed:\n"
        "    array:\n"
        "      - {}\n"
        "      - []\n",
        encoding="utf-8",
    )

    plugin = load_agent_plugin(tmp_path)

    assert plugin.apm_configuration is not None
    frozen = plugin.apm_configuration.values
    assert isinstance(frozen, FrozenJsonObject)
    dependencies = dict(frozen)["dependencies"]
    assert isinstance(dependencies, FrozenJsonObject)
    apm = dict(dependencies)["apm"]
    assert isinstance(apm, FrozenJsonArray)
    assert isinstance(apm.values[0], FrozenJsonObject)
    assert apm.values[0] != FrozenJsonArray(())
    assert frozen.thaw() == expected
    assert thaw_frozen_json(frozen) == expected
    assert load_agent_plugin(tmp_path).apm_configuration == plugin.apm_configuration


def test_frozen_json_union_is_publicly_exported() -> None:
    assert FrozenJsonArray in get_args(FrozenJsonValue)
    assert FrozenJsonObject in get_args(FrozenJsonValue)


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


@pytest.mark.parametrize(
    "schema_id",
    [
        "https://agent-plugins.org/schemas/2.0.0/plugin.schema.json",
        f"{PLUGIN_SCHEMA_ID}/",
        "https://example.com/plugin.schema.json",
    ],
)
def test_unrecognized_schema_bearing_manifest_falls_back_to_legacy_normalization(
    tmp_path: Path,
    schema_id: str,
) -> None:
    _write_manifest(tmp_path, **{"$schema": schema_id})

    detection = detect_agent_plugin(tmp_path)

    assert detection is None
    apm_yml = normalize_plugin_directory(tmp_path, tmp_path / "plugin.json")
    assert apm_yml == tmp_path / "apm.yml"
    assert apm_yml.is_file()


def test_nested_agent_plugin_manifest_cannot_bypass_root_schema_admission(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps({"$schema": PLUGIN_SCHEMA_ID, "name": "nested"}),
        encoding="utf-8",
    )

    with pytest.raises(AgentPluginError):
        normalize_plugin_directory(tmp_path, manifest)
    assert not (tmp_path / "apm.yml").exists()


def test_unknown_placeholder_shaped_values_remain_literal_in_stdio_ir(
    tmp_path: Path,
) -> None:
    _write_manifest(tmp_path)
    _write_mcp(
        tmp_path,
        {
            "literal": {
                "type": "stdio",
                "command": "tool",
                "args": ["--token", "${GITHUB_TOKEN}", "${UNKNOWN_VAR}/arg"],
                "env": {
                    "API_TOKEN": "${GITHUB_TOKEN}",
                    "CONFIG": "${UNKNOWN_VAR}/config",
                },
                "cwd": "${UNKNOWN_VAR}/work",
            }
        },
    )

    plugin = load_agent_plugin(tmp_path)

    server = plugin.components.mcp_servers[0]
    assert server.args == ("--token", "${GITHUB_TOKEN}", "${UNKNOWN_VAR}/arg")
    assert server.env == (
        ("API_TOKEN", "${GITHUB_TOKEN}"),
        ("CONFIG", "${UNKNOWN_VAR}/config"),
    )
    assert server.cwd == "${UNKNOWN_VAR}/work"
    assert plugin.diagnostics == ()


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


def test_url_and_headers_preserve_all_placeholder_shaped_literals(
    tmp_path: Path,
) -> None:
    _write_manifest(tmp_path)
    url = "https://example.com/${PLUGIN_ROOT}/${PLUGIN_DATA}/${UNKNOWN_VAR}?token=${GITHUB_TOKEN}"
    authorization = "Bearer ${GITHUB_TOKEN}"
    marker = "${PLUGIN_ROOT}:${PLUGIN_DATA}:${UNKNOWN_VAR}"
    _write_mcp(
        tmp_path,
        {
            "literal": {
                "type": "streamable-http",
                "url": url,
                "headers": {
                    "Authorization": authorization,
                    "X-Placeholders": marker,
                },
            }
        },
    )

    plugin = load_agent_plugin(tmp_path)

    server = plugin.components.mcp_servers[0]
    assert server.url == url
    assert server.headers == (
        ("Authorization", authorization),
        ("X-Placeholders", marker),
    )
    assert plugin.diagnostics == ()


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
