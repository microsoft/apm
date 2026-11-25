"""Filesystem-backed cells for the #5471 upgrade failure messages."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile, LockfileFormatError
from apm_cli.install.errors import FrozenInstallError
from apm_cli.install.request import InstallRequest
from apm_cli.install.service import InstallService
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.policy.schema import ApmPolicy, McpPolicy
from apm_cli.security.trust import McpAdmission
from apm_cli.utils.yaml_io import dump_yaml

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-install-outcome")]


def test_libyaml_lockfile_parse_error_ends_with_the_regenerate_fix(tmp_path: Path) -> None:
    lock = tmp_path / "apm.lock.yaml"
    lock.write_text("lockfile_version: '1'\ndependencies: [unclosed\n", encoding="utf-8")
    with pytest.raises(LockfileFormatError) as refusal:
        LockFile.read(lock)
    assert (
        str(refusal.value)
        .rstrip()
        .endswith("Move apm.lock.yaml aside, then run 'apm install' to regenerate apm.lock.yaml.")
    )


def test_frozen_under_blocking_mcp_policy_leads_with_the_policy_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declaration = MCPDependency(
        name="srv", registry=False, transport="http", url="https://x.invalid/srv"
    )
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    dump_yaml(
        {
            "name": "leaf",
            "version": "1.0.0",
            "dependencies": {"mcp": [declaration.to_dict()]},
        },
        leaf / "apm.yml",
    )
    mid = tmp_path / "mid"
    mid.mkdir()
    dump_yaml({"name": "mid", "version": "1.0.0"}, mid / "apm.yml")
    parent = LockedDependency(
        repo_url="_local/parent", source="local", local_path=str(mid), depth=1
    )
    child = LockedDependency(
        repo_url="_local/leaf",
        source="local",
        local_path=str(leaf),
        depth=2,
        resolved_by=parent.repo_url,
    )
    lock = LockFile(
        dependencies={d.get_unique_key(): d for d in (parent, child)},
        mcp_servers=["srv"],
        mcp_configs={"srv": declaration.to_dict()},
    )
    lock.write(tmp_path / "apm.lock.yaml")
    policy = ApmPolicy(enforcement="block", mcp=McpPolicy(trust_transitive=False))
    root = APMPackage(name="root", version="1.0.0", package_path=tmp_path)
    routed = SimpleNamespace(policy=policy, blocks=False, unresolved=False, unavailable_source=None)
    monkeypatch.setattr(
        "apm_cli.policy.outcome_routing.classify_project_policy", lambda _root: routed
    )
    with pytest.raises(FrozenInstallError) as refusal:
        InstallService.enforce_frozen(InstallRequest(apm_package=root, no_policy=False))
    headline = str(refusal.value)
    assert headline.startswith("--frozen: org MCP policy refuses")
    assert "out of sync with apm.yml" not in headline
    assert any("org policy now withholds it" in reason for reason in refusal.value.reasons)


def test_hydrated_frozen_check_under_blocking_policy_leads_with_the_policy_refusal(
    tmp_path: Path,
) -> None:
    declaration = MCPDependency(
        name="srv", registry=False, transport="http", url="https://x.invalid/srv"
    )
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    dump_yaml(
        {"name": "leaf", "version": "1.0.0", "dependencies": {"mcp": [declaration.to_dict()]}},
        leaf / "apm.yml",
    )
    mid = tmp_path / "mid"
    mid.mkdir()
    dump_yaml({"name": "mid", "version": "1.0.0"}, mid / "apm.yml")
    parent = LockedDependency(repo_url="_local/mid", source="local", local_path=str(mid), depth=1)
    child = LockedDependency(
        repo_url="_local/leaf",
        source="local",
        local_path=str(leaf),
        depth=2,
        resolved_by=parent.repo_url,
    )
    lock = LockFile(
        dependencies={d.get_unique_key(): d for d in (parent, child)},
        mcp_servers=["srv"],
        mcp_configs={"srv": declaration.to_dict()},
    )
    policy = ApmPolicy(enforcement="block", mcp=McpPolicy(trust_transitive=False))
    root = APMPackage(name="root", version="1.0.0", package_path=tmp_path)
    with pytest.raises(FrozenInstallError) as refusal:
        InstallService.enforce_hydrated_mcp(
            root,
            lock,
            tmp_path / "apm_modules",
            admission=McpAdmission(None, org_policy=lambda: policy),
            logger=None,
        )
    headline = str(refusal.value)
    assert headline.startswith("--frozen: org MCP policy refuses")
    assert "restored MCP declarations differ" not in headline
