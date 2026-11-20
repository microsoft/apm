"""APMLifecycle: an unreachable org policy is reported once and never contradicts the outcome.

PROMISE: I9 -- no duplicate or contradicting diagnostics (source: issues #4178, #4180).
- #4180: with ``policy.fetch_failure_default: block`` an install that cannot
  fetch the org policy is blocked; it must not also say it proceeds without
  policy enforcement.
- #4178: ``apm approve <pkg>`` resolves the org policy once, so its
  unreachable-policy warning appears exactly once.
CELL: OP=install|approve SRC=local SCOPE=project NET=policy-unreachable GATE=on
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_TARGET = "copilot"
# A github.com org that is never reached: every request goes to a closed proxy.
_REMOTE = "https://github.com/apm-policy-unreachable-fixture/app.git"
_DEAD_PROXY = "http://127.0.0.1:9"
_APPROVE_WARNING = "[!] Org policy not applied (outcome=cache_miss_fetch_fail,"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _flat(result: CommandResult) -> str:
    return " ".join((result.stdout + "\n" + result.stderr).split())


def _world(tmp_path: Path, *, block: bool):
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        env[name] = _DEAD_PROXY
    for name in ("NO_PROXY", "no_proxy"):
        env.pop(name, None)
    packages = LocalPackageFactory(isolated.work_root)
    executable = packages.create("p-exec")
    packages.add_hook(executable, "p-exec", {"hooks": {"sessionStart": [{"bash": "echo p-exec"}]}})
    project = packages.create("consumer", targets=(_TARGET,))
    packages.add_relative_dependency(project, executable)
    if block:
        manifest = project.root / "apm.yml"
        data = load_yaml(manifest)
        data["policy"] = {"fetch_failure_default": "block"}
        dump_yaml(data, manifest)
    for argv in (("init", "--quiet"), ("remote", "add", "origin", _REMOTE)):
        subprocess.run(("git", *argv), cwd=project.root, env=env, check=True, capture_output=True)
    return env, project


def _status_lines(result: CommandResult) -> list[str]:
    return [
        line.strip()
        for line in (result.stdout + "\n" + result.stderr).splitlines()
        if line.lstrip().startswith("[")
    ]


def test_blocked_install_does_not_claim_to_proceed_without_policy(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    env, project = _world(tmp_path, block=True)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    before = LifecycleStateSnapshot.capture(project.root, targets=(_TARGET,))

    result = runner.run(("install",), cwd=project.root, env=env)

    assert result.returncode == 1, _evidence(result)
    flat = _flat(result)
    assert flat.isascii(), _evidence(result)
    assert "policy.fetch_failure_default=block" in flat, _evidence(result)
    assert "proceeding without policy enforcement" not in flat, _evidence(result)
    lines = _status_lines(result)
    assert len(lines) == len(set(lines)), _evidence(result)
    assert LifecycleStateSnapshot.capture(project.root, targets=(_TARGET,)) == before


def test_approve_reports_unreachable_policy_once(tmp_path: Path, apm_binary_path: Path) -> None:
    env, project = _world(tmp_path, block=False)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    installed = runner.run(("install", "--no-policy"), cwd=project.root, env=env)
    assert installed.returncode == 0, _evidence(installed)

    result = runner.run(("approve", "../p-exec"), cwd=project.root, env=env)

    assert result.returncode == 0, _evidence(result)
    flat = _flat(result)
    assert flat.isascii(), _evidence(result)
    assert flat.count(_APPROVE_WARNING) == 1, _evidence(result)
    lines = _status_lines(result)
    assert len(lines) == len(set(lines)), _evidence(result)
    assert "p-exec" in (project.root / "apm.yml").read_text(encoding="utf-8")
