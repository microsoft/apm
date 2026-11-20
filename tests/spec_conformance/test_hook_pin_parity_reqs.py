"""Frozen install and audit reach one hook approval verdict (req-sc-011)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import apm_cli.security.executables as ex
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.errors import FrozenInstallError
from apm_cli.install.service import InstallService
from apm_cli.models.apm_package import APMPackage
from apm_cli.policy.ci_checks import _check_hook_approval_pins
from apm_cli.security.executables import build_exec_trust_context
from apm_cli.security.trust_fingerprint import hook_file_fingerprint
from apm_cli.utils.yaml_io import dump_yaml
from tests.spec_conformance._helpers import assert_spec_contains

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-executable-trust-fingerprint"),
]


HOOK_ITEM = ".apm/hooks/guard.json"


@pytest.fixture(autouse=True)
def _isolated_user_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ex, "_user_config_file", lambda: tmp_path / "config.json")
    monkeypatch.setattr(ex, "_legacy_approvals_path", lambda: tmp_path / "approvals.yml")


def _hook_package(root: Path) -> None:
    hooks = root / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    dump_yaml({"name": "hooky", "version": "1.0.0"}, root / "apm.yml")
    (root / "guard.sh").write_text("echo guard\n", encoding="ascii")
    document = {
        "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "./guard.sh"}]}]}
    }
    (hooks / "guard.json").write_text(json.dumps(document), encoding="ascii")


def _world(tmp_path: Path, pin: str | None):
    project = tmp_path / "project"
    project.mkdir(parents=True)
    _hook_package(tmp_path / "hooky")
    dependency = LockedDependency(
        repo_url="_local/hooky",
        source="local",
        local_path="../hooky",
        exec_status="deployed",
    )
    lock = LockFile()
    lock.add_dependency(dependency)
    modules = project / "apm_modules"
    shutil.copytree(tmp_path / "hooky", dependency.to_dependency_ref().get_install_path(modules))
    package_root = dependency.to_dependency_ref().get_install_path(modules)
    fingerprint = pin or hook_file_fingerprint(package_root, package_root / HOOK_ITEM)
    identity = ex.locked_dependency_approval_keys(dependency)[0].split("#", 1)[0]
    executables = {"allow": {identity: {"hooks": {HOOK_ITEM: fingerprint}}}}
    dump_yaml(
        {"name": "consumer", "version": "1.0.0", "executables": executables},
        project / "apm.yml",
    )
    ctx = build_exec_trust_context(policy=None, project_data={"executables": executables})
    return project, lock, modules, ctx


def _verdicts(tmp_path: Path, pin: str | None):
    project, lock, modules, ctx = _world(tmp_path, pin)
    try:
        InstallService.enforce_frozen_hook_pins(lock, modules, ctx)
        frozen: list[str] | None = None
    except FrozenInstallError as exc:
        frozen = [reason.removeprefix("  - ") for reason in exc.reasons]
    audit = _check_hook_approval_pins(
        project,
        project,
        lock,
        APMPackage.from_apm_yml(project / "apm.yml"),
        user_scope=False,
        no_policy=True,
        prepared_replay=None,
    )
    return frozen, audit


@pytest.mark.req("req-sc-011")
def test_frozen_install_and_audit_agree_on_a_hook_approval_pin(tmp_path: Path) -> None:
    assert_spec_contains(
        "install time, is verified by a frozen install against the lockfile, or",
    )
    frozen, audit = _verdicts(tmp_path / "matching", None)
    assert frozen is None and audit.passed

    frozen, audit = _verdicts(tmp_path / "changed", "sha256:" + "0" * 64)
    assert frozen is not None and not audit.passed
    assert frozen == audit.details
    assert HOOK_ITEM in frozen[0] and "definition changed since approval" in frozen[0]
