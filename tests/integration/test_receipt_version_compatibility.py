"""Run the source CLI in a disposable home; never touch live APM state."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration]


@pytest.mark.parametrize("legacy_paths", [False, True])
@pytest.mark.parametrize("user_scope", [False, True])
@pytest.mark.parametrize("edited", [False, True])
def test_real_legacy_cli_upgrade_downgrade_upgrade(
    tmp_path: Path, apm_engine_command, user_scope: bool, edited: bool, legacy_paths: bool
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
    target = "copilot" if legacy_paths else "agent-skills"
    args = (
        "install",
        *(("-g",) if user_scope else ()),
        "--target",
        target,
        "--no-policy",
        *(("--legacy-skill-paths",) if legacy_paths else ()),
    )
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
    deployed = (isolated.home if user_scope else project) / (
        (".copilot/skills/demo" if user_scope else ".github/skills/demo")
        if legacy_paths
        else ".agents/skills/demo"
    )
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
    refreshed = json.loads(old_receipt.read_text())
    assert any(
        row["value"].endswith("asset.md")
        and row["content_hash"] == "sha256:" + hashlib.sha256(b"v3\n").hexdigest()
        for row in refreshed["records"]
    )
    assert old_receipt.read_bytes() != legacy_after_downgrade
    assert json.loads(new_receipt.read_text())["version"] == 2


@pytest.mark.parametrize("legacy_paths", [False, True])
@pytest.mark.parametrize("user_scope", [False, True])
@pytest.mark.parametrize("edited", [False, True])
def test_fresh_fixed_install_can_downgrade_without_losing_skills(
    tmp_path: Path, apm_engine_command, user_scope: bool, edited: bool, legacy_paths: bool
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
    (package / "apm.yml").write_text("name: demo-pkg\nversion: 1.0.0\n")
    skill_text = "---\nname: demo\ndescription: Fresh downgrade fixture\n---\nv1\n"
    (source / "SKILL.md").write_text(skill_text)
    (source / "asset.md").write_text("v1\n")
    (root / "apm.yml").write_text(
        f"name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n  - path: {package}\ntargets:\n- agent-skills\n"
    )
    fixed_binary = os.environ.get("APM_FIXED_BINARY")
    fixed = ApmLifecycleRunner((fixed_binary,) if fixed_binary else apm_engine_command)
    legacy = ApmLifecycleRunner((legacy_binary,))
    target = "copilot" if legacy_paths else "agent-skills"
    args = (
        "install",
        *(("-g",) if user_scope else ()),
        "--target",
        target,
        "--no-policy",
        *(("--legacy-skill-paths",) if legacy_paths else ()),
    )
    result = fixed.run((*args, "--dry-run"), cwd=project, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list((isolated.home / ".apm").glob("deployment-receipts*/*.json"))
    result = fixed.run(args, cwd=project, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    receipts = list((isolated.home / ".apm").glob("deployment-receipts*/*.json"))
    proof = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in receipts}
    result = fixed.run(args, cwd=project, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in receipts} == proof
    deployed = (isolated.home if user_scope else project) / (
        (".copilot/skills/demo" if user_scope else ".github/skills/demo")
        if legacy_paths
        else ".agents/skills/demo"
    )
    if edited:
        (deployed / "SKILL.md").write_text("local edits\n")
    before = {p.name: p.read_bytes() for p in deployed.iterdir()}
    lock_before = (root / "apm.lock.yaml").read_bytes()
    (package / "apm.yml").write_text("name: demo-pkg\nversion: 1.1.0\n")
    (source / "SKILL.md").write_text(skill_text.replace("v1", "v2"))
    (source / "asset.md").write_text("v2\n")
    result = legacy.run(args, cwd=project, env=env)
    assert deployed.is_dir(), result.stdout + result.stderr
    assert all((deployed / name).is_file() for name in before), result.stdout + result.stderr
    if result.returncode:
        assert {p.name: p.read_bytes() for p in deployed.iterdir()} == before
        assert (root / "apm.lock.yaml").read_bytes() == lock_before
    else:
        assert (deployed / "SKILL.md").read_text() == (
            "local edits\n" if edited else (source / "SKILL.md").read_text()
        )
        assert (deployed / "asset.md").read_bytes() == (source / "asset.md").read_bytes()
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(p.parent.name == "deployment-receipts" for p in receipts)


@pytest.mark.parametrize("user_scope", [False, True])
@pytest.mark.parametrize("legacy_paths", [False, True])
@pytest.mark.parametrize("member_added", [False, True])
def test_legacy_uninstall_revokes_newer_v2_authority_after_claim_restore(
    tmp_path: Path,
    apm_engine_command,
    monkeypatch,
    user_scope: bool,
    legacy_paths: bool,
    member_added: bool,
):
    """Actual old uninstall must not leave authority for a recreated newer file."""
    from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
    from apm_cli.core.scope import InstallScope
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.targets import KNOWN_TARGETS, apply_legacy_skill_paths

    legacy_binary = os.environ.get("APM_LEGACY_BINARY")
    if not legacy_binary:
        pytest.skip("Set APM_LEGACY_BINARY to the unpatched APM 0.35.0 executable")
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    if legacy_paths:
        env["APM_LEGACY_SKILL_PATHS"] = "1"
    project = isolated.work_root / "consumer"
    project.mkdir()
    root = isolated.config_root if user_scope else project
    package = isolated.work_root / "package"
    source = package / ".apm/skills/demo"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: Uninstall fixture\n---\nv1\n")
    if not member_added:
        (source / "asset.md").write_text("v1\n")
    (package / "apm.yml").write_text("name: demo-pkg\nversion: 1.0.0\n")
    target_name = "copilot" if legacy_paths else "agent-skills"
    manifest = root / "apm.yml"
    manifest.write_text(
        f"name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n  - path: {package}\ntargets:\n- {target_name}\n"
    )
    scope_args = ("-g",) if user_scope else ()
    fixed = ApmLifecycleRunner(apm_engine_command)
    legacy = ApmLifecycleRunner((legacy_binary,))
    args = ("install", *scope_args, "--target", target_name, "--no-policy")
    for version in (1, 2):
        if not member_added or version == 2:
            (source / "asset.md").write_text(f"v{version}\n")
        (package / "apm.yml").write_text(f"name: demo-pkg\nversion: 1.{version}.0\n")
        result = fixed.run(args, cwd=project, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = next((isolated.config_root / "deployment-receipts").glob("*.json"))
        asset_rows = [
            row
            for row in json.loads(receipt.read_text())["records"]
            if row["value"].endswith("asset.md")
        ]
        assert bool(asset_rows) == (not member_added or version == 2)
    lock_path = root / "apm.lock.yaml"
    lock_bytes, manifest_bytes = lock_path.read_bytes(), manifest.read_bytes()
    target = KNOWN_TARGETS[target_name].for_scope(user_scope=user_scope)
    assert target is not None
    if legacy_paths:
        target = apply_legacy_skill_paths([target])[0]
    deploy_root = isolated.home if user_scope else project
    asset = target.skills_deploy_path(deploy_root) / "demo/asset.md"
    assert asset.read_text() == "v2\n"
    result = legacy.run(("uninstall", *scope_args, str(package)), cwd=project, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not asset.exists(), "The old executable must actually delete the newer bytes"
    # Restore historical branch claims and recreate the former newer bytes.
    manifest.write_bytes(manifest_bytes)
    lock_path.write_bytes(lock_bytes)
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_text("v2\n")
    (source / "asset.md").write_text("v3\n")
    legacy_receipt = next((isolated.config_root / "deployment-receipts").glob("*.json"))
    legacy_proof = legacy_receipt.read_bytes()
    assert json.loads(legacy_proof)["records"] == []
    scope = InstallScope.USER if user_scope else InstallScope.PROJECT
    # Check the authority boundary directly: #3439 remains independent of the
    # refusal/cleanup error gate supplied separately by #3440.
    with monkeypatch.context() as patch:
        patch.setattr(Path, "home", classmethod(lambda cls: isolated.home))
        for _ in range(2):
            lock = LockFile.read(lock_path)
            session = DeploymentMaterializationSession(deploy_root, scope, lock, targets=[target])
            assert session.baseline(asset, [target]) is None
            session.bind_committed_claims(lock)
            assert asset.read_text() == "v2\n"
            assert lock_path.read_bytes() == lock_bytes
            assert legacy_receipt.read_bytes() == legacy_proof


@pytest.mark.parametrize("user_scope", [False, True])
@pytest.mark.parametrize("legacy_paths", [False, True])
def test_new_uninstall_retires_v1_before_downgrade_recreation(
    tmp_path: Path, apm_engine_command, user_scope: bool, legacy_paths: bool
):
    """An actual downgraded reader cannot reuse a retired compatibility baseline."""
    from apm_cli.integration.targets import KNOWN_TARGETS, apply_legacy_skill_paths

    legacy_binary = os.environ.get("APM_LEGACY_BINARY")
    if not legacy_binary:
        pytest.skip("Set APM_LEGACY_BINARY to the unpatched APM 0.35.0 executable")
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    if legacy_paths:
        env["APM_LEGACY_SKILL_PATHS"] = "1"
    project = isolated.work_root / "consumer"
    project.mkdir()
    root = isolated.config_root if user_scope else project
    package = isolated.work_root / "package"
    source = package / ".apm/skills/demo"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: Uninstall fixture\n---\nv1\n")
    (source / "asset.md").write_text("v1\n")
    (package / "apm.yml").write_text("name: demo-pkg\nversion: 1.0.0\n")
    target_name = "copilot" if legacy_paths else "agent-skills"
    manifest = root / "apm.yml"
    manifest.write_text(
        f"name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n  - path: {package}\ntargets:\n- {target_name}\n"
    )
    scope_args = ("-g",) if user_scope else ()
    fixed = ApmLifecycleRunner(apm_engine_command)
    legacy = ApmLifecycleRunner((legacy_binary,))
    args = ("install", *scope_args, "--target", target_name, "--no-policy")
    for version in (1, 2):
        (source / "asset.md").write_text(f"v{version}\n")
        (package / "apm.yml").write_text(f"name: demo-pkg\nversion: 1.{version}.0\n")
        result = fixed.run(args, cwd=project, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
    lock_path = root / "apm.lock.yaml"
    lock_bytes, manifest_bytes = lock_path.read_bytes(), manifest.read_bytes()
    target = KNOWN_TARGETS[target_name].for_scope(user_scope=user_scope)
    assert target is not None
    if legacy_paths:
        target = apply_legacy_skill_paths([target])[0]
    deploy_root = isolated.home if user_scope else project
    asset = target.skills_deploy_path(deploy_root) / "demo/asset.md"
    assert asset.read_text() == "v2\n"
    result = fixed.run(("uninstall", *scope_args, str(package)), cwd=project, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not asset.exists()
    receipts = list(isolated.config_root.glob("deployment-receipts*/*.json"))
    assert len(receipts) == 2
    assert all(json.loads(receipt.read_text())["records"] == [] for receipt in receipts)
    manifest.write_bytes(manifest_bytes)
    lock_path.write_bytes(lock_bytes)
    asset.parent.mkdir(parents=True, exist_ok=True)
    # Recreate the bytes formerly authorized by v1, distinct from the later
    # lock's v2 hash. This isolates stale overwrite authority from the old
    # executable's separate refusal/cleanup bug repaired by #3440.
    asset.write_text("v1\n")
    (source / "asset.md").write_text("v3\n")
    (package / "apm.yml").write_text("name: demo-pkg\nversion: 1.3.0\n")
    result = legacy.run(args, cwd=project, env=env)
    assert asset.is_file(), result.stdout + result.stderr
    assert asset.read_text() == "v1\n", result.stdout + result.stderr
    assert "no trustworthy local deployment baseline" in " ".join(
        (result.stdout + result.stderr).split()
    )
    legacy_receipt = next((isolated.config_root / "deployment-receipts").glob("*.json"))
    assert all(
        not row["value"].endswith("asset.md")
        for row in json.loads(legacy_receipt.read_text())["records"]
    )


def test_legacy_uninstall_revokes_relocated_user_target(
    tmp_path: Path, apm_engine_command, monkeypatch
):
    from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
    from apm_cli.core.scope import InstallScope
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.targets import KNOWN_TARGETS

    legacy_binary = os.environ.get("APM_LEGACY_BINARY")
    if not legacy_binary:
        pytest.skip("Set APM_LEGACY_BINARY to the unpatched APM 0.35.0 executable")
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    project.mkdir()
    package = isolated.work_root / "package"
    source = package / ".apm/skills/demo"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: Relocation fixture\n---\nv1\n")
    (source / "asset.md").write_text("v2\n")
    (package / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")
    manifest = isolated.config_root / "apm.yml"
    manifest.write_text(
        f"name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n  - path: {package}\ntargets:\n- claude\n"
    )
    fixed, old = ApmLifecycleRunner(apm_engine_command), ApmLifecycleRunner((legacy_binary,))
    args = ("install", "-g", "--target", "claude", "--no-policy")
    for name in ("original-target", "moved-target"):
        env["CLAUDE_CONFIG_DIR"] = str(isolated.home / name)
        result = fixed.run(args, cwd=project, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
    lock_path = isolated.config_root / "apm.lock.yaml"
    lock_bytes, manifest_bytes = lock_path.read_bytes(), manifest.read_bytes()
    asset = isolated.home / "moved-target/skills/demo/asset.md"
    assert asset.read_text() == "v2\n"
    receipt = next((isolated.config_root / "deployment-receipts").glob("*.json"))
    assert str(asset) in json.loads(receipt.read_text())["destinations"].values()
    before_outputs = {
        str(path): path.read_bytes()
        for name in ("original-target", "moved-target")
        for path in (isolated.home / name / "skills/demo").glob("*.md")
    }
    result = old.run(("uninstall", "-g", str(package)), cwd=project, env=env)
    if result.returncode:
        # Some legacy configurations reject these relocated target paths
        # before deletion. That is a safe stop, not retirement evidence.
        assert result.returncode == 1
        assert "could not remove tracked target files" in result.stdout
        assert lock_path.read_bytes() == lock_bytes
        assert manifest.read_bytes() == manifest_bytes
        assert {path: Path(path).read_bytes() for path in before_outputs} == before_outputs
        return
    assert not asset.exists()
    manifest.write_bytes(manifest_bytes)
    lock_path.write_bytes(lock_bytes)
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_text("v2\n")
    (source / "asset.md").write_text("v3\n")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "home", classmethod(lambda cls: isolated.home))
        patch.setenv("CLAUDE_CONFIG_DIR", env["CLAUDE_CONFIG_DIR"])
        target = KNOWN_TARGETS["claude"].for_scope(user_scope=True)
        assert target is not None
        for _ in range(2):
            lock = LockFile.read(lock_path)
            session = DeploymentMaterializationSession(
                isolated.home, InstallScope.USER, lock, targets=[target]
            )
            assert session.baseline(asset, [target]) is None
            session.bind_committed_claims(lock)
            assert asset.read_text() == "v2\n"
