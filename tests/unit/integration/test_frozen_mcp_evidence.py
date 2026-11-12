"""Frozen MCP evidence distinguishes unavailable sources from verified declarations."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.errors import FrozenInstallError
from apm_cli.install.service import InstallService
from apm_cli.integration.mcp_config_view import CurrentMcpConfigView
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from apm_cli.utils.yaml_io import dump_yaml

pytestmark = pytest.mark.component

_CONFIG = {"name": "tool", "transport": "stdio", "registry": False, "command": "inert"}


def _baseline(
    tmp_path: Path, package_type: str = "marketplace_plugin"
) -> tuple[APMPackage, LockFile]:
    """Build focused source evidence without invoking a native client."""
    root = APMPackage(name="root", version="1.0.0", package_path=tmp_path)
    dependency = LockedDependency(
        repo_url="owner/plugin",
        name="declared-name",
        package_type=package_type,
        resolved_commit="a" * 40,
        resolved_ref="v1",
        depth=1,
    )
    lock = LockFile(
        dependencies={dependency.get_unique_key(): dependency},
        mcp_configs={"tool": _CONFIG},
        mcp_servers=["tool"],
        mcp_config_provenance={"tool": "declared-name"},
    )
    return root, lock


def _view(root: APMPackage, lock: LockFile, *, allow_pending: bool) -> CurrentMcpConfigView:
    """Select explicit frozen evidence mode rather than compatibility traversal."""
    return CurrentMcpConfigView.derive(
        root,
        lock,
        root.package_path / "apm_modules",
        trust_transitive_self_defined=False,
        allow_pending=allow_pending,
    )


@pytest.mark.parametrize(
    "package_type",
    [
        "apm_package",
        "marketplace_plugin",
        "agent_plugin",
        "skill_bundle",
        "claude_skill",
        "hybrid",
        "hook_package",
    ],
)
def test_absent_supported_shapes_are_pending_not_verified(
    tmp_path: Path, package_type: str
) -> None:
    root, lock = _baseline(tmp_path, package_type)
    current = _view(root, lock, allow_pending=True)
    assert current.pending_packages == frozenset({"owner/plugin"})
    assert current.pending_servers(lock) == frozenset({"tool"})
    assert current.configs == {}
    assert current.diff(lock.mcp_configs).lock_only == frozenset({"tool"})
    assert not current.problems
    strict = _view(root, lock, allow_pending=False)
    assert len(strict.problems) == 1
    assert not strict.pending_packages


@pytest.mark.parametrize(
    "override",
    [
        {"package_type": "unknown"},
        {"package_type": "invalid"},
        {"resolved_commit": None},
        {"resolved_commit": "main"},
        {"source": "registry", "version": "1.0.0"},
        {"source": "local", "local_path": "./missing", "package_type": "skill_bundle"},
    ],
)
def test_insufficient_or_local_evidence_is_never_deferred(tmp_path: Path, override: dict) -> None:
    root, lock = _baseline(tmp_path)
    dependency = replace(next(iter(lock.dependencies.values())), **override)
    lock.dependencies = {dependency.get_unique_key(): dependency}
    current = _view(root, lock, allow_pending=True)
    assert len(current.problems) == 1
    assert not current.pending_packages
    assert not current.pending_servers(lock)


def test_complete_registry_evidence_is_pending_not_verified(tmp_path: Path) -> None:
    """A complete registry pin permits acquisition, not assumed MCP declarations."""
    root, lock = _baseline(tmp_path, "apm_package")
    dependency = replace(
        lock.dependencies["owner/plugin"],
        source="registry",
        version="1.0.0",
        resolved_url="https://registry.example.invalid/plugin-1.0.0.tar.gz",
        resolved_hash="sha256:" + "a" * 64,
    )
    lock.dependencies = {dependency.get_unique_key(): dependency}
    current = _view(root, lock, allow_pending=True)
    assert current.pending_packages == frozenset({dependency.get_unique_key()})
    assert not current.problems
    assert current.configs == {}
    assert current.pending_servers(lock) == frozenset({"tool"})
    assert len(_view(root, lock, allow_pending=False).problems) == 1


def test_active_missing_source_is_not_an_inactive_orphan(tmp_path: Path) -> None:
    """Full graph membership excludes only orphans, never unhydrated active sources."""
    root, lock = _baseline(tmp_path, "apm_package")
    orphan = replace(lock.dependencies["owner/plugin"], repo_url="owner/orphan", name="unused")
    lock.dependencies[orphan.get_unique_key()] = orphan
    current = CurrentMcpConfigView.derive(
        root,
        lock,
        tmp_path / "apm_modules",
        trust_transitive_self_defined=False,
        allow_pending=False,
        active_package_keys=frozenset({"owner/plugin"}),
    )
    assert [problem.package_key for problem in current.problems] == ["owner/plugin"]
    assert current.configs == {}
    with pytest.raises(FrozenInstallError, match="restored MCP declarations differ") as failure:
        InstallService.enforce_hydrated_mcp(
            root,
            lock,
            tmp_path / "apm_modules",
            trust_transitive_mcp=False,
            logger=None,
            active_package_keys=frozenset({"owner/plugin"}),
        )
    assert any(
        "owner/plugin: package manifest not found" in reason for reason in failure.value.reasons
    )
    assert all("owner/orphan" not in reason for reason in failure.value.reasons)


@pytest.mark.parametrize(
    "case", ["missing-name", "coincidental-directory", "shared-owner", "ambiguous-name"]
)
def test_deferred_origin_never_guesses_an_owner(tmp_path: Path, case: str) -> None:
    root, lock = _baseline(tmp_path)
    dependency = lock.dependencies["owner/plugin"]
    if case == "missing-name":
        dependency.name = None
    elif case == "coincidental-directory":
        lock.mcp_config_provenance = {"tool": "plugin"}
    elif case == "shared-owner":
        lock.mcp_config_provenance = {"tool": ["declared-name", "present-owner"]}
    else:
        second = replace(dependency, repo_url="another/plugin")
        lock.dependencies[second.get_unique_key()] = second
    current = _view(root, lock, allow_pending=True)
    assert current.pending_packages
    assert not current.pending_servers(lock)


@pytest.mark.parametrize("shape", ["skill_bundle", "marketplace_plugin", "agent_plugin", "hybrid"])
def test_materialized_invalid_shape_is_not_a_cold_cache(tmp_path: Path, shape: str) -> None:
    root, lock = _baseline(tmp_path, shape)
    (tmp_path / "apm_modules/owner/plugin").mkdir(parents=True)
    current = _view(root, lock, allow_pending=True)
    assert len(current.problems) == 1
    assert not current.pending_packages


@pytest.mark.parametrize("command", [None, "changed", "inert"])
def test_hydrated_comparison_reads_actual_source(tmp_path: Path, command: str | None) -> None:
    root, lock = _baseline(tmp_path, "apm_package")
    directory = tmp_path / "apm_modules/owner/plugin"
    directory.mkdir(parents=True)
    dump_yaml(
        {
            "name": "declared-name",
            "version": "1.0.0",
            "dependencies": {"mcp": [{**_CONFIG, "command": command}]} if command else {},
        },
        directory / "apm.yml",
    )
    clear_apm_yml_cache()
    if command == "inert":
        InstallService.enforce_hydrated_mcp(
            root, lock, tmp_path / "apm_modules", trust_transitive_mcp=False, logger=None
        )
        assert _view(root, lock, allow_pending=False).configs == lock.mcp_configs
    else:
        with pytest.raises(FrozenInstallError, match="restored MCP declarations differ") as failure:
            InstallService.enforce_hydrated_mcp(
                root, lock, tmp_path / "apm_modules", trust_transitive_mcp=False, logger=None
            )
        assert any("tool" in reason for reason in failure.value.reasons)


def test_pending_source_cannot_hide_present_shared_server_drift(tmp_path: Path) -> None:
    root, lock = _baseline(tmp_path)
    present = LockedDependency(
        repo_url="owner/present", name="present-owner", package_type="apm_package", depth=1
    )
    lock.dependencies[present.get_unique_key()] = present
    lock.mcp_config_provenance["tool"] = ["declared-name", "present-owner"]
    directory = tmp_path / "apm_modules/owner/present"
    directory.mkdir(parents=True)
    dump_yaml(
        {
            "name": "present-owner",
            "version": "1.0.0",
            "dependencies": {"mcp": [{**_CONFIG, "command": "changed"}]},
        },
        directory / "apm.yml",
    )
    current = _view(root, lock, allow_pending=True)
    assert current.pending_packages == frozenset({"owner/plugin"})
    assert not current.pending_servers(lock)
    assert current.diff(lock.mcp_configs).changed == frozenset({"tool"})


def test_manifestless_native_plugin_keeps_its_mcp_ownership(tmp_path: Path) -> None:
    """Inspect a valid native shape without launching a native runtime or translating MCP."""
    root, lock = _baseline(tmp_path)
    directory = tmp_path / "apm_modules/owner/plugin"
    metadata = directory / ".codex-plugin/plugin.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps({"name": "declared-name", "version": "1.0.0", "mcpServers": "./.mcp.json"}),
        encoding="utf-8",
    )
    native_mcp = directory / ".mcp.json"
    native_mcp.write_text(json.dumps({"mcpServers": {"tool": _CONFIG}}), encoding="utf-8")
    before = {path: path.read_bytes() for path in (metadata, native_mcp)}
    current = _view(root, lock, allow_pending=False)
    assert current.problems == ()
    assert current.configs == {}
    assert not current.pending_packages
    assert not (directory / "apm.yml").exists()
    assert {path: path.read_bytes() for path in before} == before


def test_empty_locked_mcp_state_still_verifies_hydrated_declarations(tmp_path: Path) -> None:
    root, lock = _baseline(tmp_path, "apm_package")
    lock.mcp_configs = {}
    lock.mcp_servers = []
    lock.mcp_config_provenance = {}
    manifest = tmp_path / "apm_modules/owner/plugin/apm.yml"
    manifest.parent.mkdir(parents=True)
    dump_yaml(
        {"name": "declared-name", "version": "1.0.0", "dependencies": {"mcp": [_CONFIG]}},
        manifest,
    )
    with pytest.raises(FrozenInstallError, match="restored MCP declarations differ") as failure:
        InstallService.enforce_hydrated_mcp(
            root, lock, tmp_path / "apm_modules", trust_transitive_mcp=False, logger=None
        )
    assert failure.value.reasons == ["  - MCP server 'tool' is missing from apm.lock.yaml"]
