"""Tests for bundle-owned MCP lifecycle reconciliation."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.install.mcp.integration import run_owned_mcp_integration
from apm_cli.models.dependency.mcp import MCPDependency

pytestmark = pytest.mark.component


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


@pytest.mark.parametrize("conflict", [False, True])
def test_real_bundle_reconciliation_preserves_other_and_deferred_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: bool
) -> None:
    """Exercise the real bundle owner, installer, adapters, and lock codec together."""
    from apm_cli.install.errors import MCPNativeConflictError
    from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

    isolated = IsolatedApmEnvironment.create(tmp_path / "bundle", base_env={})
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in isolated.subprocess_env().items():
        monkeypatch.setenv(key, value)
    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / ".claude").mkdir()
    monkeypatch.chdir(project)
    lock_path = project / "apm.lock.yaml"

    def install(owner: str, names: list[str]) -> int:
        return run_owned_mcp_integration(
            dependencies=[
                MCPDependency.from_dict(
                    {
                        "name": name,
                        "registry": False,
                        "transport": "http",
                        "url": f"http://127.0.0.1:8000/{name}",
                    }
                )
                for name in names
            ],
            owner=owner,
            lock_path=lock_path,
            project_root=project,
            user_scope=False,
            target_runtimes=["claude"],
        )

    assert install("b", ["other"]) == 1
    assert install("a", ["old", "clash"]) == 2
    path = project / ".mcp.json"
    native = json.loads(path.read_text(encoding="utf-8"))
    if conflict:
        native["mcpServers"]["clash"]["url"] = "http://127.0.0.1:9/personal"
        path.write_text(json.dumps(native), encoding="utf-8")
        with pytest.raises(MCPNativeConflictError) as caught:
            install("a", ["new", "clash"])
        assert caught.value.wrote_connections
    else:
        assert install("a", ["new"]) == 1
    updated = LockFile.read(lock_path)
    assert updated is not None
    expected = ["new", "old", "other"] if conflict else ["new", "other"]
    assert updated.mcp_target_servers == {"claude": expected}
    assert updated.mcp_config_provenance["other"] == "bundle:b"
    actual = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
    assert actual["other"] == native["mcpServers"]["other"]
    assert set(actual) == ({"new", "old", "other", "clash"} if conflict else {"new", "other"})
    if conflict:
        assert actual["clash"] == native["mcpServers"]["clash"]
