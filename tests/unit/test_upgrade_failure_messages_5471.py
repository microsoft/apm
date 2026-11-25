"""The four v0.39 upgrade failure messages name the exact fix (#5471)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.deps.git_remote_ops import exact_branch_or_tag_sha
from apm_cli.deps.lockfile import LockedDependency, LockFile, LockfileFormatError
from apm_cli.install.errors import FrozenInstallError
from apm_cli.install.request import InstallRequest
from apm_cli.install.service import InstallService
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.policy.policy_checks import mcp_policy_checks
from apm_cli.policy.schema import ApmPolicy, McpPolicy
from apm_cli.utils.yaml_io import dump_yaml

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-install-outcome")]


def test_short_sha_refusal_asks_for_the_full_sha_not_a_sha() -> None:
    records = [("a" * 40, "refs/heads/main")]
    with pytest.raises(RuntimeError) as refusal:
        exact_branch_or_tag_sha(records, "abc1234", "example.com/o/r")
    text = str(refusal.value)
    assert "full 40-character SHA" in text
    assert "or a branch or tag" in text
    assert "use a commit SHA" not in text.lower()


def test_trust_transitive_audit_summary_names_the_alternative() -> None:
    server = MCPDependency(name="srv", registry=False, transport="http", url="https://x.invalid/s")
    server.resolved_by = "leaf"
    server.declared_depth = 2
    policy = ApmPolicy(enforcement="warn", mcp=McpPolicy(trust_transitive=False))
    check = {c.name: c for c in mcp_policy_checks([server], policy)}["mcp-trust-transitive"]
    assert not check.passed
    assert "add them to mcp.allow or set mcp.trust_transitive: true" in check.message


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
