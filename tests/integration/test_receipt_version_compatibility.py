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
def test_real_legacy_cli_upgrade_downgrade_upgrade(
    tmp_path: Path, apm_engine_command, user_scope: bool, edited: bool
):
    legacy_binary = os.environ.get("APM_LEGACY_BINARY")
    if not legacy_binary:
        pytest.skip("Set APM_LEGACY_BINARY to the unpatched APM 0.35.0 executable")
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    project.mkdir(parents=True)
    root = isolated.config_root if user_scope else project
    root.mkdir(parents=True, exist_ok=True)
    package = isolated.work_root / "package"
    source = package / ".apm/skills/demo"
    source.mkdir(parents=True)
    skill = source / "SKILL.md"
    skill.write_text("---\nname: demo\ndescription: Compatibility fixture\n---\nv1\n")
    (package / "apm.yml").write_text("name: demo-pkg\nversion: 1.0.0\n")
    (root / "apm.yml").write_text(
        f"name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n  - path: {package}\ntargets:\n- agent-skills\n"
    )
    args = ("install", *(("-g",) if user_scope else ()), "--target", "agent-skills", "--no-policy")
    legacy = ApmLifecycleRunner((legacy_binary,))
    fixed_binary = os.environ.get("APM_FIXED_BINARY")
    fixed = ApmLifecycleRunner((fixed_binary,) if fixed_binary else apm_engine_command)

    def install(runner, *extra):
        result = runner.run((*args, *extra), cwd=project, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    install(legacy)
    old_receipt = next((isolated.home / ".apm/deployment-receipts").glob("*.json"))
    old_bytes = old_receipt.read_bytes()
    new_receipt = isolated.home / ".apm/deployment-receipts-v2" / old_receipt.name
    deployed = (isolated.home if user_scope else project) / ".agents/skills/demo"
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in deployed.iterdir()}
    install(fixed, "--dry-run")
    assert not new_receipt.exists()
    assert old_receipt.read_bytes() == old_bytes
    install(fixed)
    assert new_receipt.exists(), "An unchanged live install must migrate the valid old receipt"
    assert old_receipt.read_bytes() == old_bytes
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in deployed.iterdir()} == before
    if edited:
        (deployed / "SKILL.md").write_text("local edits\n")
    # Old APM must still read its own receipt and make real, witnessed writes.
    skill.write_text(skill.read_text().replace("v1", "v2"))
    (source / "asset.md").write_text("v2\n")
    install(legacy)
    legacy_after_downgrade = old_receipt.read_bytes()
    assert json.loads(legacy_after_downgrade)["version"] == 1
    assert (deployed / "asset.md").read_text() == "v2\n"
    assert (deployed / "SKILL.md").read_text() == ("local edits\n" if edited else skill.read_text())
    # Returning to fixed APM must recognize changed older-writer evidence,
    # while an unchanged older baseline must not authorize a user's edits.
    skill.write_text(skill.read_text().replace("v2", "v3"))
    (source / "asset.md").write_text("v3\n")
    install(fixed)
    assert (deployed / "asset.md").read_text() == "v3\n"
    assert (deployed / "SKILL.md").read_text() == ("local edits\n" if edited else skill.read_text())
    assert old_receipt.read_bytes() == legacy_after_downgrade
    assert json.loads(new_receipt.read_text())["version"] == 2
