"""Hermetic install/audit regression for ownership sidecar object-key order."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.integration


def test_preexisting_hook_event_order_does_not_cause_sidecar_drift(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    factory = LocalPackageFactory(isolated.work_root)
    project = factory.create("hook-order", targets=("claude",))
    factory.add_instruction(project, "rules", '---\napplyTo: "**"\n---\nBe brief.\n')
    user_hook = {"hooks": [{"type": "command", "command": "echo user"}]}
    settings_path = project.root / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({"hooks": {"SessionStart": [user_hook]}}), encoding="utf-8")
    factory.add_hook(
        project,
        "events",
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo tool"}]}
                ],
                "SessionStart": [{"hooks": [{"type": "command", "command": "echo start"}]}],
            }
        },
    )
    runner = ApmLifecycleRunner(apm_engine_command)
    install_args = ("install", "--no-policy", "--parallel-downloads", "0")
    first = runner.run(install_args, cwd=project.root, env=environment)
    assert first.returncode == 0, first.stdout + first.stderr
    sidecar_path = project.root / ".claude" / "apm-hooks.json"
    installed_bytes = sidecar_path.read_bytes()
    assert list(json.loads(installed_bytes)) == ["SessionStart", "PreToolUse"]

    second = runner.run(install_args, cwd=project.root, env=environment)
    assert second.returncode == 0, second.stdout + second.stderr
    assert sidecar_path.read_bytes() == installed_bytes
    before = LifecycleStateSnapshot.capture(project.root, targets=("claude",))

    result = runner.run(
        ("audit", "--ci", "--no-policy", "--format", "json"),
        cwd=project.root,
        env=environment,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["drift"]["drift"] == []
    assert LifecycleStateSnapshot.capture(project.root, targets=("claude",)) == before
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert user_hook in settings["hooks"]["SessionStart"]
