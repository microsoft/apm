"""Installed-CLI contract: a denied canvas names the deny, not apm approve (#4052).

A dependency whose canvas extension is hard-denied (``apm deny``) must get a
skip line that points to ``apm policy explain``: approving cannot lift a deny.
Control: an unapproved, undenied canvas keeps the approvable "not approved /
apm approve" line. Since #3290 a Claude plugin with bin/ at user scope loads
natively, so its deny is reported by the native-plugin owner; that path is a
control here. Deployed files and the lockfile ``exec_status`` follow the gate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml
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
def test_native_bin_plugin_denied_at_user_scope_names_the_deny(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Control: a Claude plugin with bin/ at -g is loaded natively (#3290).

    Its deny is reported by the native-plugin owner, which already points to
    ``apm policy explain``; this passes before and after #4052. User scope is
    not governed by org policy (#4087), so the deny comes from the user store.
    """
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    isolated = world.isolated
    plugin = isolated.work_root / "tool"
    _bin_plugin(plugin)
    package = str(plugin)
    dump_yaml(
        {
            "name": "user",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": [package]},
            "executables": {"allow": {package: {"bin": True}}},
        },
        isolated.config_root / "apm.yml",
    )
    (isolated.home / ".claude").mkdir(parents=True, exist_ok=True)
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        # The package label is an absolute path: keep it on one console line.
        env = {**fx.child_environment(world.git_env()), "COLUMNS": "1000"}

        def apm(*args: str, step: str) -> str:
            result = run_step(runner, args, cwd=isolated.work_root, env=env, step=step)
            return _flat(output(result))

        first = apm("install", "-g", "--trust-bin", step="1-install")
        assert "native Claude plugin(s) declared and enabled" in first, first

        apm("deny", "--user", package, step="2-deny")
        denied = apm("install", "-g", "--trust-bin", step="3-install-denied")
        assert f"Parked native plugin {package}: denied" in denied, denied
        # A user-scope hint names the -g form, which works from any directory (#4154).
        assert f"apm policy explain -g {package}" in denied, denied


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
