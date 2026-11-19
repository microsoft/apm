"""Installed-CLI contract: a gated package's deployed hooks retire (#3924).

When the executables gate stops deploying a package's hooks -- ``apm deny``,
or an approval removed while the gate is on -- the next ``apm install`` must
remove the hook entries and copied scripts that package deployed earlier,
from every harness config, while user-authored hooks stay. ``apm audit --ci``
must agree with that state, and the skip message must name a denied package
as denied instead of telling the user to approve it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_SCRIPT = "hooky-guard.sh"
_USER_COMMAND = "echo user-owned-hook"


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


def _project_files(project: Path) -> list[Path]:
    return [
        path
        for path in project.rglob("*")
        if path.is_file()
        and "apm_modules" not in path.relative_to(project).parts
        and path.name not in {"apm.yml", "apm.lock.yaml"}
    ]


def _hook_residue(project: Path) -> list[str]:
    """Return every deployed file that still names or is the package script."""
    residue = []
    for path in _project_files(project):
        rel = path.relative_to(project).as_posix()
        if path.name == _SCRIPT or _SCRIPT in path.read_text(encoding="utf-8"):
            residue.append(rel)
    return sorted(residue)


def _claude_commands(project: Path) -> list[str]:
    settings = project / ".claude" / "settings.json"
    if not settings.is_file():
        return []
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [
        handler.get("command", "")
        for groups in data.get("hooks", {}).values()
        for group in groups
        for handler in group.get("hooks", [])
    ]


def _exec_status(project: Path) -> str | None:
    lock = LockFile.read(project / "apm.lock.yaml")
    assert lock is not None
    (dependency,) = lock.dependencies.values()
    return dependency.exec_status


@pytest.mark.parametrize(
    ("target", "gate_off"),
    [("claude", "deny"), ("claude", "revoke"), ("copilot", "deny")],
    ids=["claude-deny", "claude-revoke", "copilot-deny"],
)
def test_gating_a_package_retires_its_deployed_hooks(
    tmp_path: Path, apm_binary_path: Path, target: str, gate_off: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    _hook_package(isolated.work_root / "hooky")
    project = isolated.work_root / "consumer"
    project.mkdir()
    manifest = {
        "name": "consumer",
        "version": "0.1.0",
        "targets": [target],
        "dependencies": {"apm": ["../hooky"]},
    }
    if gate_off == "revoke":
        manifest["executables"] = {"allow": {"../hooky": {"hooks": True}}}
    dump_yaml(manifest, project / "apm.yml")
    if target == "claude":
        user_hook = {"hooks": [{"type": "command", "command": _USER_COMMAND}]}
        (project / ".claude").mkdir()
        (project / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"PreToolUse": [user_hook]}}, indent=2) + "\n",
            encoding="ascii",
        )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def apm(*args: str, expect: int = 0) -> str:
        result = runner.run(args, cwd=project, env=env)
        output = result.stdout + result.stderr
        assert result.returncode == expect, f"apm {' '.join(args)}\n{output}"
        return output

    apm("install", "--no-policy")
    assert _hook_residue(project), "the hook must deploy before it is gated"
    deployed_configs = {
        path: path.read_bytes() for path in _project_files(project) if path.suffix == ".json"
    }

    if gate_off == "deny":
        apm("deny", "../hooky")
    else:
        dump_yaml({**manifest, "executables": {}}, project / "apm.yml")

    gated = " ".join(apm("install", "--no-policy").split())
    assert "hooks skipped" in gated, gated
    if gate_off == "deny":
        assert "hooks skipped (denied)" in gated, gated
        assert "apm approve" not in gated, gated
        assert _exec_status(project) == "denied"
    else:
        assert "not approved in allowExecutables" in gated, gated
        assert _exec_status(project) == "gated_pending_approval"

    assert _hook_residue(project) == []
    if target == "claude":
        assert _claude_commands(project) == [_USER_COMMAND]

    audit = apm("audit", "--ci", "--no-policy")
    assert "Drift detected" not in audit, audit

    if target == "claude":
        # A denied hook still registered (e.g. by an older CLI) fails the audit.
        for path, content in deployed_configs.items():
            path.write_bytes(content)
        leftover = apm("audit", "--ci", "--no-policy", expect=1)
        assert "Drift detected" in leftover, leftover
        assert ".claude/settings.json" in leftover, leftover
