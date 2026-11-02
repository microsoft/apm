"""Run the source CLI in a disposable home; never touch live APM state."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration]


@pytest.mark.parametrize("user_scope", [False, True])
@pytest.mark.parametrize("edited", [False, True])
@pytest.mark.parametrize("package_kind", ["apm_package", "claude_skill", "skill_bundle"])
def test_missing_receipt_cannot_turn_refused_writes_into_stale_deletions(
    tmp_path: Path, apm_engine_command, user_scope: bool, edited: bool, package_kind: str
):
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    project.mkdir(parents=True)
    root = isolated.config_root if user_scope else project
    root.mkdir(parents=True, exist_ok=True)
    package = isolated.work_root / "package"
    source = (
        package
        / {"apm_package": ".apm/skills/demo", "claude_skill": "", "skill_bundle": "skills/demo"}[
            package_kind
        ]
    )
    source.mkdir(parents=True)
    if package_kind != "claude_skill":
        (package / "apm.yml").write_text("name: demo-pkg\nversion: 1.0.0\n")
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: Receipt fixture\n---\nv1\n")
    (source / "a.md").write_text("v1\n")
    (source / "b.md").write_text("v1\n")
    (root / "apm.yml").write_text(
        f"name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n  - path: {package}\ntargets:\n- agent-skills\n"
    )
    fixed_binary = os.environ.get("APM_FIXED_BINARY")
    runner = ApmLifecycleRunner((fixed_binary,) if fixed_binary else apm_engine_command)
    args = ("install", *(("-g",) if user_scope else ()), "--target", "agent-skills", "--no-policy")
    first = runner.run(args, cwd=project, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    deployed_skills = list(
        ((isolated.home if user_scope else project) / ".agents/skills").glob("*/SKILL.md")
    )
    assert len(deployed_skills) == 1
    deployed = deployed_skills[0].parent
    if edited:
        (deployed / "SKILL.md").write_text("my local edits\n")
    before = {p.name: p.read_bytes() for p in deployed.iterdir()}
    lock_before = (root / "apm.lock.yaml").read_bytes()
    receipts = list((isolated.home / ".apm").glob("deployment-receipts*/*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["version"] in (1, 2)
    receipts[0].unlink()
    if package_kind != "claude_skill":
        (package / "apm.yml").write_text("name: demo-pkg\nversion: 1.1.0\n")
    (source / "a.md").write_text("v2\n")
    (source / "b.md").unlink()
    (source / "c.md").write_text("v2\n")
    for _ in range(2):
        result = runner.run(args, cwd=project, env=env)
        assert result.returncode != 0, result.stdout + result.stderr
        assert "no trustworthy local deployment baseline" in " ".join(
            (result.stdout + result.stderr).split()
        )
        assert "stale files cleaned" not in result.stdout
        for name, content in before.items():
            assert (deployed / name).read_bytes() == content
        assert (root / "apm.lock.yaml").read_bytes() == lock_before
