"""Installed-CLI evidence for the proposed general uninstall reachability duty."""

import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration import test_required_lifecycle_state_machine as lifecycle
from tests.integration.conftest import apm_binary_path as apm_binary_path
from tests.utils.artifact_snapshot import (
    ArtifactSnapshot,
    ArtifactSnapshotSet,
    assert_snapshot_set_unchanged,
    assert_unchanged,
)

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]


@pytest.mark.req("req-lk-024")
@pytest.mark.parametrize("source_kind", ["local", "remote"])
@pytest.mark.parametrize("first_parent", ["root-a", "root-b"])
def test_sequential_diamond_lifecycle(
    tmp_path: Path, apm_binary_path: Path, source_kind: str, first_parent: str
) -> None:
    """Bind the existing lifecycle oracle, including anchors, bytes and both previews."""
    lifecycle.test_required_diamond_uninstall_preserves_shared_instructions_until_last_parent(
        tmp_path, apm_binary_path, first_parent, source_kind
    )


@pytest.mark.req("req-lk-024")
@pytest.mark.parametrize("source_kind", ["local", "remote"])
@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "malformed",
        pytest.param(
            "unreadable",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="chmod cannot establish POSIX read denial on Windows"
            ),
        ),
    ],
)
def test_uncertain_manifest_preserves_material_and_preview(
    tmp_path: Path, apm_binary_path: Path, source_kind: str, damage: str
) -> None:
    """An unreadable subtree is not an empty subtree, for preview or real removal."""
    fixture = lifecycle._diamond_fixture(tmp_path, apm_binary_path, source_kind)
    scenario, consumer = fixture.scenario, fixture.consumer
    lifecycle._run_success(
        scenario,
        consumer,
        (*lifecycle._INSTALL_ARGS, "--target", "copilot"),
        environment=fixture.environment,
        scenario_id="uncertain-install",
    )
    lock_path = consumer.root / "apm.lock.yaml"
    lock = LockFile.read(lock_path)
    assert lock is not None
    dependencies = {dep.repo_url: dep for dep in lock.get_package_dependencies()}
    shared = dependencies[f"{fixture.prefix}/shared"]
    modules = consumer.root / "apm_modules"
    shared_install = shared.to_dependency_ref().get_install_path(modules)
    material = ArtifactSnapshot.capture(shared_install)
    installed_instruction = consumer.root / ".github/instructions/shared.instructions.md"
    instruction_bytes = installed_instruction.read_bytes()
    user_note = consumer.root / ".github/instructions/personal.txt"
    user_note.write_bytes(b"Personal content is not an orphan.\n")
    survivor = (
        fixture.parents[1].root
        if source_kind == "local"
        else dependencies[f"{fixture.prefix}/root-b"].to_dependency_ref().get_install_path(modules)
    )
    manifest = survivor / "apm.yml"
    manifest_bytes = manifest.read_bytes()
    if damage == "missing":
        manifest.unlink()
    elif damage == "malformed":
        manifest.write_bytes(b"name: [unterminated\n")
    else:
        manifest.chmod(0)
    roots = {"project": consumer.root, "user": scenario.isolated.home}
    # Permission-denied source files cannot themselves be hashed. Snapshot the
    # affected project's bytes and check the denied file after restoring access.
    try:
        if damage == "unreadable" and source_kind == "remote":
            manifest.chmod(0o600)
        before = ArtifactSnapshotSet.capture(roots)
        if damage == "unreadable":
            manifest.chmod(0)
        preview = scenario.runner.run(
            ("uninstall", fixture.parent_arguments["root-a"], "--dry-run"),
            cwd=consumer.root,
            env=fixture.environment,
            scenario_id="uncertain-preview",
        )
        if damage == "unreadable":
            manifest.chmod(0o600)
        assert_snapshot_set_unchanged(before, ArtifactSnapshotSet.capture(roots))
        if damage == "unreadable":
            manifest.chmod(0)
        removal = scenario.runner.run(
            ("uninstall", fixture.parent_arguments["root-a"]),
            cwd=consumer.root,
            env=fixture.environment,
            scenario_id="uncertain-remove",
        )
    finally:
        if damage == "unreadable":
            manifest.chmod(0o600)
            assert manifest.read_bytes() == manifest_bytes

    after = LockFile.read(lock_path)
    assert after is not None
    retained = {dep.repo_url: dep for dep in after.get_package_dependencies()}
    assert f"{fixture.prefix}/shared" in retained
    assert retained[f"{fixture.prefix}/shared"].to_dict() == shared.to_dict()
    assert_unchanged(material, ArtifactSnapshot.capture(shared_install))
    assert installed_instruction.read_bytes() == instruction_bytes
    assert user_note.read_bytes() == b"Personal content is not an orphan.\n"
    for result in (preview, removal):
        output = " ".join((result.stdout + result.stderr).split()).lower()
        if damage == "unreadable":
            assert result.returncode != 0
            assert "permission denied" in output
            assert "apm.yml" in output
        else:
            assert "could not be verified" in output
            assert "fix or restore" in output
    assert "Transitive dependencies that would be removed:" not in preview.stdout


@pytest.mark.req("req-lk-024")
@pytest.mark.parametrize("damage", ["unsafe", "ambiguous"])
def test_uncertain_path_preserves_candidate_and_external_content(
    tmp_path: Path, apm_binary_path: Path, damage: str
) -> None:
    """Do not convert unsafe containment or ambiguous local ancestry to deletion."""
    source_kind = "remote" if damage == "unsafe" else "local"
    fixture = lifecycle._diamond_fixture(tmp_path, apm_binary_path, source_kind)
    scenario, consumer = fixture.scenario, fixture.consumer
    lifecycle._run_success(
        scenario,
        consumer,
        lifecycle._INSTALL_ARGS,
        environment=fixture.environment,
        scenario_id="uncertain-path-install",
    )
    lock_path = consumer.root / "apm.lock.yaml"
    lock = LockFile.read(lock_path)
    assert lock is not None
    dependencies = {dep.repo_url: dep for dep in lock.get_package_dependencies()}
    shared = dependencies[f"{fixture.prefix}/shared"]
    modules = consumer.root / "apm_modules"
    shared_install = shared.to_dependency_ref().get_install_path(modules)
    material = ArtifactSnapshot.capture(shared_install)
    instruction = consumer.root / ".github/instructions/shared.instructions.md"
    instruction_bytes = instruction.read_bytes()
    user_note = consumer.root / ".github/instructions/personal.txt"
    user_note.write_bytes(b"Protected personal bytes.\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "personal.txt").write_bytes(b"Outside APM ownership.\n")
    if damage == "unsafe":
        survivor_install = (
            dependencies[f"{fixture.prefix}/root-b"].to_dependency_ref().get_install_path(modules)
        )
        survivor_install.rename(outside / "saved-survivor")
        survivor_install.symlink_to(outside / "saved-survivor", target_is_directory=True)
    else:
        data = load_yaml(lock_path)
        parent = next(dep for dep in data["dependencies"] if dep["repo_url"] == "_local/root-a")
        data["dependencies"].append({**parent, "local_path": "../ambiguous/root-a"})
        dump_yaml(data, lock_path)
    roots = {"project": consumer.root, "user": scenario.isolated.home, "outside": outside}
    before = ArtifactSnapshotSet.capture(roots)
    preview = scenario.runner.run(
        ("uninstall", fixture.parent_arguments["root-a"], "--dry-run"),
        cwd=consumer.root,
        env=fixture.environment,
        scenario_id="uncertain-path-preview",
    )
    assert_snapshot_set_unchanged(before, ArtifactSnapshotSet.capture(roots))
    removal = scenario.runner.run(
        ("uninstall", fixture.parent_arguments["root-a"]),
        cwd=consumer.root,
        env=fixture.environment,
        scenario_id="uncertain-path-remove",
    )
    after = LockFile.read(lock_path)
    assert after is not None
    assert any(dep.to_dict() == shared.to_dict() for dep in after.get_package_dependencies())
    assert_unchanged(material, ArtifactSnapshot.capture(shared_install))
    assert instruction.read_bytes() == instruction_bytes
    assert user_note.read_bytes() == b"Protected personal bytes.\n"
    assert_unchanged(before.snapshot("outside"), ArtifactSnapshot.capture(outside))
    for result in (preview, removal):
        output = " ".join((result.stdout + result.stderr).split()).lower()
        assert "could not be verified" in output
        assert "fix or restore" in output
    assert "Transitive dependencies that would be removed:" not in preview.stdout
