"""Portable, content-bound transitive MCP consent (req-sc-016; req-sc-011 frozen)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import apm_cli.security.executables as ex
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.integration.mcp_config_view import CurrentMcpConfigView
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.policy.schema import ApmPolicy, ExecutablesPolicy
from apm_cli.security.executables import build_exec_trust_context
from apm_cli.security.trust import McpAdmission
from apm_cli.security.trust_fingerprint import mcp_definition_fingerprint
from apm_cli.utils.yaml_io import dump_yaml
from tests.spec_conformance._helpers import assert_spec_contains

pytestmark = pytest.mark.component

SERVER = {"name": "leaf-server", "registry": False, "transport": "stdio", "command": "npx"}


@pytest.fixture(autouse=True)
def _isolated_user_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "config.json")
    monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "approvals.yml")


def _project(tmp_path: Path, declaration: dict = SERVER, depth: int = 2):
    dump_yaml({"name": "consumer", "version": "1.0.0"}, tmp_path / "apm.yml")
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    dump_yaml(
        {"name": "leaf", "version": "1.0.0", "dependencies": {"mcp": [declaration]}},
        leaf / "apm.yml",
    )
    dependency = LockedDependency(
        repo_url="_local/leaf", source="local", local_path=str(leaf), depth=depth
    )
    lock = LockFile()
    lock.add_dependency(dependency)
    root = APMPackage.from_apm_yml(tmp_path / "apm.yml")
    return root, lock, dependency


def _view(tmp_path: Path, root, lock, admission: McpAdmission) -> CurrentMcpConfigView:
    return CurrentMcpConfigView.derive(root, lock, tmp_path / "apm_modules", admission=admission)


def _pinned(dependency: LockedDependency, fingerprint: str, policy=None):
    identity = ex.locked_dependency_approval_keys(dependency)[0].split("#", 1)[0]
    allow = {identity: {"mcp": {"leaf-server": fingerprint}}}
    return build_exec_trust_context(policy=policy, project_data={"executables": {"allow": allow}})


@pytest.mark.req("req-sc-016")
def test_transitive_mcp_consent_comes_from_a_pinned_manifest_grant(tmp_path: Path) -> None:
    assert_spec_contains(
        "**[req-sc-016]** A conforming **consumer** implementation MUST admit an",
        "fingerprint of the server definition that matches the current",
        "MUST NOT derive that consent from the lockfile, MUST NOT persist a",
        "the declaring package and the server. A grant that binds a fingerprint",
    )
    root, lock, dependency = _project(tmp_path)
    pin = mcp_definition_fingerprint(MCPDependency.from_dict(SERVER))

    unapproved = _view(
        tmp_path, root, lock, McpAdmission(build_exec_trust_context(policy=None, project_data={}))
    )
    assert "leaf-server" not in unapproved.configs
    assert unapproved.omissions["leaf-server"]["reason"] == "untrusted-transitive"

    approved = _view(tmp_path, root, lock, McpAdmission(_pinned(dependency, pin)))
    assert "leaf-server" in approved.configs

    one_off = _view(tmp_path, root, lock, McpAdmission(None, one_off=True))
    assert "leaf-server" in one_off.configs
    assert [name for name, _package in one_off.one_off_admitted] == ["leaf-server"]


@pytest.mark.req("req-sc-016")
def test_per_user_pin_admits_and_the_opt_in_never_overrides_a_stale_pin(tmp_path: Path) -> None:
    assert_spec_contains(
        "approval location of [req-sc-010](#req-sc-010), binds the declaring",
        "a full-length `sha256:<64 lowercase hex>` digest compared in full, never",
        "fingerprint no longer matches, even under (c); in that case it MUST",
    )
    root, lock, dependency = _project(tmp_path)
    pin = mcp_definition_fingerprint(MCPDependency.from_dict(SERVER))
    identity = ex.locked_dependency_approval_keys(dependency)[0].split("#", 1)[0]
    grant = {"allow": {identity: {"mcp": {"leaf-server": pin}}}, "deny": {}}
    (tmp_path / "config.json").write_text(json.dumps({"executables": grant}), encoding="utf-8")
    user_only = build_exec_trust_context(policy=None, project_data={})
    assert "leaf-server" in _view(tmp_path, root, lock, McpAdmission(user_only)).configs
    (tmp_path / "config.json").unlink()

    stale = _pinned(dependency, "sha256:" + "0" * 64)
    view = _view(tmp_path, root, lock, McpAdmission(stale, one_off=True))
    assert "leaf-server" not in view.configs
    assert view.omissions["leaf-server"]["reason"] == "definition-changed"


@pytest.mark.req("req-sc-016")
def test_lockfile_state_never_grants_transitive_consent(tmp_path: Path) -> None:
    root, lock, _dependency = _project(tmp_path)
    lock.mcp_servers = ["leaf-server"]
    lock.mcp_configs = {"leaf-server": dict(SERVER)}
    view = _view(tmp_path, root, lock, McpAdmission(None))
    assert "leaf-server" not in view.configs
    assert "not approved in apm.yml" in view.consent_problems(lock)["leaf-server"]
    audited = view.with_recorded_omissions(lock)
    assert any("not approved in apm.yml" in problem.message for problem in audited.problems)


@pytest.mark.req("req-sc-016")
def test_changed_definition_is_withheld_with_a_named_diagnostic(tmp_path: Path) -> None:
    changed = {**SERVER, "args": ["-y", "evil@1"]}
    root, lock, dependency = _project(tmp_path, changed)
    pin = mcp_definition_fingerprint(MCPDependency.from_dict(SERVER))
    warnings: list[str] = []
    logger = SimpleNamespace(
        warning=warnings.append, progress=lambda _m: None, verbose_detail=lambda _m: None
    )
    view = CurrentMcpConfigView.derive(
        root,
        lock,
        tmp_path / "apm_modules",
        admission=McpAdmission(_pinned(dependency, pin)),
        logger=logger,
    )
    assert "leaf-server" not in view.configs
    assert view.omissions["leaf-server"]["reason"] == "definition-changed"
    identity = ex.locked_dependency_approval_keys(dependency)[0].split("#", 1)[0]
    assert any("'leaf-server'" in w and f"apm approve {identity}" in w for w in warnings), warnings


@pytest.mark.req("req-sc-016")
def test_organization_denial_still_wins_over_a_pin(tmp_path: Path) -> None:
    root, lock, dependency = _project(tmp_path)
    pin = mcp_definition_fingerprint(MCPDependency.from_dict(SERVER))
    policy = ApmPolicy(executables=ExecutablesPolicy(deny_all=True))
    view = _view(tmp_path, root, lock, McpAdmission(_pinned(dependency, pin, policy)))
    assert view.omissions["leaf-server"]["reason"] == "denied"


@pytest.mark.req("req-sc-011")
def test_frozen_install_reaches_the_install_verdict(tmp_path: Path) -> None:
    from apm_cli.install.service import InstallService

    assert_spec_contains(
        "install time, is verified by a frozen install against the lockfile, or",
    )
    root, lock, dependency = _project(tmp_path, depth=1)
    ctx = build_exec_trust_context(policy=None, project_data={"executables": {}})
    # Install parks the unapproved direct dependency's server; frozen must agree.
    view = _view(tmp_path, root, lock, McpAdmission(ctx))
    assert view.omissions["leaf-server"]["reason"] == "unapproved-executables"
    lock.mcp_omissions = view.omission_evidence(lock)
    reasons = InstallService._frozen_mcp_reasons(
        root,
        lock,
        tmp_path / "apm_modules",
        admission=McpAdmission(ctx),
        logger=None,
        allow_pending=False,
    )
    assert reasons == []
    assert dependency.depth == 1
