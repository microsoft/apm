"""Installed-CLI contract: denied bin/ and canvas name the deny, not apm approve (#4052).

A dependency whose bin/ or canvas extension is hard-denied (org policy or
``apm deny``) must get a skip line that points to ``apm policy explain``:
approving cannot lift a deny. Controls: an unapproved, undenied bin/ or canvas
keeps the approvable "not approved / apm approve" line, and an approved bin/
without ``--trust-bin`` in a non-interactive run keeps the "not trusted" line.
Deployed files and the lockfile ``exec_status`` follow the gate as before.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._policy_scope_kit import ORG, PolicyScopeWorld, output, run_step
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(os.name == "nt", reason="POSIX bin/ fixture"),
]

_BASE_POLICY = {"name": "contoso-policy", "version": "1.0.0", "enforcement": "block"}
_DENY_ALL = {**_BASE_POLICY, "executables": {"deny_all": True}}


def _flat(text: str) -> str:
    return " ".join(text.split())


def _denied_line(kind: str, package: str) -> str:
    return f"{kind} skipped (denied). Run 'apm policy explain {package}'"


def _approve_hint(package: str) -> str:
    return f"Run 'apm approve {package}' to approve."


def _exec_status(lock_path: Path) -> str | None:
    lock = LockFile.read(lock_path)
    assert lock is not None
    (dependency,) = lock.dependencies.values()
    return dependency.exec_status


# -- bin/ (Claude plugin at user scope, org policy) ---------------------------


def _bin_plugin(root: Path) -> None:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "tool", "version": "1.0.0", "description": "bin fixture"}),
        encoding="utf-8",
    )
    skill = root / "skills" / "tool"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: tool\ndescription: bin fixture skill\n---\n# tool\n", encoding="utf-8"
    )
    (root / "bin").mkdir()
    tool = root / "bin" / "tool"
    tool.write_text("#!/bin/sh\necho tool\n", encoding="utf-8")
    tool.chmod(0o755)


@pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available")
@pytest.mark.parametrize("case", ["org-deny", "unapproved", "not-trusted"])
def test_bin_skip_line_names_the_deny(tmp_path: Path, apm_binary_path: Path, case: str) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    world.publish_policy(_BASE_POLICY)
    isolated = world.isolated
    plugin = isolated.work_root / "tool"
    _bin_plugin(plugin)
    package = str(plugin)
    grant = {} if case == "unapproved" else {package: {"bin": True}}
    dump_yaml(
        {
            "name": "user",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": [package]},
            "executables": {"allow": grant},
        },
        isolated.config_root / "apm.yml",
    )
    # User-scope org policy is discovered from the deploy root ($HOME).
    (isolated.home / ".claude").mkdir(parents=True, exist_ok=True)
    git_env = isolated.subprocess_env()
    for args in (
        ["git", "init", "-q", "--initial-branch=main"],
        ["git", "remote", "add", "origin", f"https://github.com/{ORG}/home.git"],
    ):
        subprocess.run(args, cwd=isolated.home, env=git_env, check=True)
    deployed = isolated.home / ".claude" / "skills" / "tool" / "bin" / "tool"
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        # The package label is an absolute path: keep it on one console line.
        env = {**fx.child_environment(world.git_env()), "COLUMNS": "1000"}

        def apm(*args: str, step: str) -> str:
            result = run_step(runner, args, cwd=isolated.work_root, env=env, step=step)
            return _flat(output(result))

        trust = () if case == "not-trusted" else ("--trust-bin",)
        first = apm("install", "-g", *trust, step="1-install")
        if case == "unapproved":
            assert "bin/ executables skipped (not approved" in first, first
            assert _approve_hint(package) in first, first
            assert not deployed.exists()
            return
        if case == "not-trusted":
            assert "bin/ executables skipped (not trusted)" in first, first
            assert f"apm install --trust-bin {package}" in first, first
            assert not deployed.exists()
            return
        assert deployed.is_file(), first

        world.publish_policy(_DENY_ALL)
        world.clear_policy_cache()
        denied = apm("install", "-g", "--trust-bin", step="2-install-denied")
        assert _denied_line("bin/ executables", package) in denied, denied
        assert _approve_hint(package) not in denied, denied
        assert not deployed.exists()
        assert _exec_status(isolated.config_root / "apm.lock.yaml") == "denied"


# -- canvas (project scope, apm deny) -----------------------------------------


def _canvas_package(root: Path) -> None:
    extension = root / ".apm" / "extensions" / "board"
    extension.mkdir(parents=True)
    dump_yaml({"name": "canvy", "version": "1.0.0"}, root / "apm.yml")
    (extension / "extension.mjs").write_text("export default {};\n", encoding="utf-8")


@pytest.mark.parametrize("case", ["deny", "unapproved"])
def test_canvas_skip_line_names_the_deny(tmp_path: Path, apm_binary_path: Path, case: str) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    isolated = world.isolated
    _canvas_package(isolated.work_root / "canvy")
    package = "../canvy"
    grant = {} if case == "unapproved" else {package: {"canvas": True}}
    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [package]},
            "executables": {"allow": grant},
        },
        project / "apm.yml",
    )
    env = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)
    deployed = project / ".github" / "extensions" / "board" / "extension.mjs"

    def apm(*args: str, step: str) -> str:
        return _flat(output(run_step(runner, args, cwd=project, env=env, step=step)))

    apm("experimental", "enable", "canvas", step="0-enable-canvas")
    first = apm("install", "--no-policy", step="1-install")
    if case == "unapproved":
        assert "canvas extension(s) skipped (not approved" in first, first
        assert _approve_hint(package) in first, first
        assert not deployed.exists()
        return
    assert deployed.is_file(), first

    apm("deny", package, step="2-deny")
    denied = apm("install", "--no-policy", step="3-install-denied")
    assert _denied_line("canvas extension(s)", package) in denied, denied
    assert _approve_hint(package) not in denied, denied
    assert not deployed.exists()
    assert _exec_status(project / "apm.lock.yaml") == "denied"
