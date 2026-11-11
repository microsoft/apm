"""Source-CLI contracts for missing-receipt skill preservation (#3372)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.component, pytest.mark.integration, pytest.mark.lifecycle_smoke]


def _skill(directory: Path) -> None:
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Cold replay fixture\n---\nDemo.\n", encoding="ascii"
    )
    (directory / "asset.md").write_text("Original asset.\n", encoding="ascii")


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize("frozen", [True, False], ids=["frozen", "ordinary"])
@pytest.mark.parametrize("edited", [False, True], ids=["unchanged", "edited"])
@pytest.mark.parametrize("fresh_identity", [False, True], ids=["same-checkout", "fresh-clone"])
def test_cold_overlapping_skill_replay_preserves_files_and_lock(
    tmp_path: Path,
    apm_engine_command: tuple[str, ...],
    user_scope: bool,
    frozen: bool,
    edited: bool,
    fresh_identity: bool,
) -> None:
    """Replay duplicate skill delivery without modules or per-user receipts."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    state_root = isolated.config_root if user_scope else project
    deploy_root = isolated.home if user_scope else project
    package = isolated.work_root / "pkg"
    skill = package / ".apm/skills/demo"
    _skill(skill)
    dump_yaml({"name": "pkg", "version": "1.0.0"}, package / "apm.yml")
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot", "claude"],
            "dependencies": {
                "apm": [
                    {"path": str(package), "skills": ["demo"]},
                    {"path": str(skill), "targets": ["claude"]},
                ]
            },
        },
        state_root / "apm.yml",
    )
    runner = ApmLifecycleRunner(apm_engine_command)
    env = isolated.subprocess_env()
    install = ("install", *(("--global",) if user_scope else ()), "--no-policy")
    initial = runner.run(install, cwd=project, env=env)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    roots = (deploy_root / ".agents/skills", deploy_root / ".claude/skills")
    assert all((root / "demo/SKILL.md").is_file() for root in roots)
    if edited:
        (roots[1] / "demo/SKILL.md").write_text("Personal changes.\n", encoding="ascii")
    before = [ArtifactSnapshot.capture(root) for root in roots]
    lock_path = state_root / "apm.lock.yaml"
    lock_before = lock_path.read_bytes()
    shutil.rmtree(state_root / "apm_modules")
    receipts = list((isolated.home / ".apm").glob("deployment-receipts*/*.json"))
    assert receipts
    for receipt in receipts:
        receipt.unlink()
    if fresh_identity:
        (deploy_root / ".apm-local/installation").unlink()
    replay = (*install, *(("--frozen",) if frozen else ()))
    for _ in range(2):
        result = runner.run(replay, cwd=project, env=env)
        assert result.returncode != 0, result.stdout + result.stderr
        output = " ".join((result.stdout + result.stderr).split())
        assert "no trustworthy local deployment baseline" in output
        assert (
            "Move the conflicting file aside and retry; "
            "use --force only to intentionally overwrite it." in output
        ), output
        assert "stale files cleaned" not in result.stdout
        assert lock_path.read_bytes() == lock_before
        for snapshot, root in zip(before, roots, strict=True):
            assert_unchanged(snapshot, ArtifactSnapshot.capture(root))


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize("kind", ["apm", "native", "bundle"])
@pytest.mark.parametrize("edited", [False, True], ids=["unchanged", "edited"])
@pytest.mark.parametrize("secondary_only", [False, True], ids=["both-targets", "secondary"])
def test_missing_receipt_errors_reach_install_from_every_skill_target(
    tmp_path: Path,
    apm_engine_command: tuple[str, ...],
    user_scope: bool,
    kind: str,
    edited: bool,
    secondary_only: bool,
) -> None:
    """A successful primary copy cannot mask a refused secondary deployment."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    state_root = isolated.config_root if user_scope else project
    deploy_root = isolated.home if user_scope else project
    package = isolated.work_root / "pkg"
    source = package / {"apm": ".apm/skills/demo", "native": "", "bundle": "skills/demo"}[kind]
    _skill(source)
    if kind != "native":
        dump_yaml({"name": "pkg", "version": "1.0.0"}, package / "apm.yml")
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["agent-skills", "claude"],
            "dependencies": {"apm": [{"path": str(package)}]},
        },
        state_root / "apm.yml",
    )
    runner = ApmLifecycleRunner(apm_engine_command)
    env = isolated.subprocess_env()
    install = (
        "install",
        *(("--global",) if user_scope else ()),
        "--target",
        "agent-skills,claude",
        "--no-policy",
    )
    initial = runner.run(install, cwd=project, env=env)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    primary = deploy_root / ".agents/skills"
    secondary = deploy_root / ".claude/skills"
    assert len(list(primary.glob("*/SKILL.md"))) == 1
    deployed = list(secondary.glob("*/SKILL.md"))
    assert len(deployed) == 1
    if secondary_only:
        shutil.rmtree(primary)
    if edited:
        deployed[0].write_text("Personal changes.\n", encoding="ascii")
    before = ArtifactSnapshot.capture(secondary)
    lock_path = state_root / "apm.lock.yaml"
    lock_before = lock_path.read_bytes()
    receipts = list((isolated.home / ".apm").glob("deployment-receipts*/*.json"))
    assert receipts
    for receipt in receipts:
        receipt.unlink()
    (source / "asset.md").unlink()
    (source / "new.md").write_text("New asset.\n", encoding="ascii")
    for _ in range(2):
        result = runner.run(install, cwd=project, env=env)
        assert result.returncode != 0, result.stdout + result.stderr
        output = " ".join((result.stdout + result.stderr).split())
        assert "no trustworthy local deployment baseline" in output
        assert "[x] 1 package failed:" in output, output
        assert f"/{package.name} -- Skipped" in output, output
        difference = before.diff(ArtifactSnapshot.capture(secondary))
        assert difference.removed == frozenset()
        assert difference.changed == frozenset()
        assert "stale files cleaned" not in result.stdout
        assert lock_path.read_bytes() == lock_before
