"""Tests for bundle-owned MCP lifecycle reconciliation."""

from unittest.mock import patch

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.install.mcp.integration import run_owned_mcp_integration
from apm_cli.models.dependency.mcp import MCPDependency


def _dependency(name: str) -> MCPDependency:
    return MCPDependency(name=name, transport="stdio", command="echo")


def test_owned_mcp_rejects_name_claimed_by_another_bundle(tmp_path) -> None:
    lock_path = tmp_path / "apm.lock.yaml"
    lockfile = LockFile()
    lockfile.mcp_config_provenance = {"shared": "bundle:bundle-a"}
    lockfile.write(lock_path)

    with pytest.raises(ValueError, match="conflicts with another owner"):
        run_owned_mcp_integration(
            dependencies=[_dependency("shared")],
            owner="bundle-b",
            lock_path=lock_path,
            project_root=tmp_path,
            user_scope=False,
            target_runtimes=["codex"],
        )


def test_owned_mcp_reconciles_stale_servers_and_preserves_other_owners(tmp_path) -> None:
    lock_path = tmp_path / "apm.lock.yaml"
    lockfile = LockFile()
    lockfile.mcp_config_provenance = {
        "old": "bundle:bundle-a",
        "other": "bundle:bundle-b",
    }
    lockfile.mcp_configs = {"old": {"name": "old"}, "other": {"name": "other"}}
    lockfile.mcp_target_servers = {
        "codex": {"old", "other"},
        "vscode": {"old"},
    }
    lockfile.write(lock_path)

    def fake_install(dependencies, **kwargs):
        managed = kwargs["managed_target_servers"]
        managed.setdefault("codex", set()).update(dep.name for dep in dependencies)
        return len(dependencies)

    with (
        patch(
            "apm_cli.integration.mcp_integrator.MCPIntegrator.install",
            side_effect=fake_install,
        ),
        patch("apm_cli.integration.mcp_integrator.MCPIntegrator.remove_stale") as remove_stale,
    ):
        count = run_owned_mcp_integration(
            dependencies=[_dependency("new")],
            owner="bundle-a",
            lock_path=lock_path,
            project_root=tmp_path,
            user_scope=False,
            target_runtimes=["codex"],
        )

    updated = LockFile.read(lock_path)
    assert updated is not None
    assert count == 1
    assert updated.mcp_config_provenance == {
        "new": "bundle:bundle-a",
        "other": "bundle:bundle-b",
    }
    assert updated.mcp_target_servers == {"codex": ["new", "other"]}
    assert remove_stale.call_args.kwargs["scope"].value == "project"


def test_owned_mcp_empty_update_removes_only_bundle_state(tmp_path) -> None:
    lock_path = tmp_path / "apm.lock.yaml"
    lockfile = LockFile()
    lockfile.mcp_config_provenance = {
        "old": "bundle:bundle-a",
        "other": "bundle:bundle-b",
    }
    lockfile.mcp_target_servers = {"codex": {"old", "other"}}
    lockfile.write(lock_path)

    count = run_owned_mcp_integration(
        dependencies=[],
        owner="bundle-a",
        lock_path=lock_path,
        project_root=tmp_path,
        user_scope=False,
        target_runtimes=["codex"],
    )

    updated = LockFile.read(lock_path)
    assert updated is not None
    assert count == 0
    assert updated.mcp_config_provenance == {
        "other": "bundle:bundle-b",
    }
    assert updated.mcp_target_servers == {"codex": ["other"]}
