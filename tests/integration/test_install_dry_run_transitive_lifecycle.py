"""Offline installed-CLI preview agreement and non-mutation contracts for #3035."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from tests.unit.copilot_plugins._builders import write_agent_plugin
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, ArtifactSnapshotSet
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]


@pytest.mark.parametrize("state", ["required", "orphan", "unknown"])
def test_transitive_preview_matches_install_without_mutation(
    tmp_path: Path, apm_binary_path: Path, state: str
) -> None:
    """Required children, removed roots, and unknown subtrees remain distinct."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    child = factory.create("child")
    parent = factory.create("parent", dependencies=("../child",))
    consumer = factory.create("consumer", dependencies=("../parent",), targets=("copilot",))
    for package in (parent, child):
        factory.add_instruction(package, package.name, '---\napplyTo: "**"\n---\n# Instruction\n')
    environment = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    install = ("install", "--target", "copilot", "--no-policy")

    if state == "required":
        # First process startup may create Python/version-check caches, not install state.
        cold_roots = {
            "sources-and-deployments": isolated.work_root,
            "configuration": isolated.config_root,
        }
        cold_before = ArtifactSnapshotSet.capture(cold_roots)
        cold = runner.run((*install, "--dry-run"), cwd=consumer.root, env=environment)
        assert cold.returncode == 0, cold.stdout + cold.stderr
        assert "APM dependencies (2)" in cold.stdout
        assert ArtifactSnapshotSet.capture(cold_roots) == cold_before

    initial = runner.run(install, cwd=consumer.root, env=environment)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    child_file = consumer.root / ".github/instructions/child.instructions.md"
    assert child_file.is_file()
    child_bytes = child_file.read_bytes()
    if state == "orphan":
        factory.replace_apm_dependencies(consumer, ())
    elif state == "unknown":
        parent.manifest_path.write_text("dependencies: [broken\n", encoding="utf-8")

    before = ArtifactSnapshot.capture(isolated.root)
    lifecycle_before = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    preview = runner.run((*install, "--dry-run"), cwd=consumer.root, env=environment)
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert ArtifactSnapshot.capture(isolated.root) == before
    assert LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",)) == lifecycle_before
    assert child_file.read_bytes() == child_bytes

    if state == "unknown":
        assert "Dependency preview incomplete" in preview.stdout
        assert "Files that would be removed" not in preview.stdout
        assert "Dry run completed: would" not in preview.stdout
        return
    if state == "required":
        assert "APM dependencies (2)" in preview.stdout
        assert "../child -> install" in preview.stdout
        assert "Files that would be removed" not in preview.stdout
    else:
        assert "APM dependencies (" not in preview.stdout
        assert "Files that would be removed" in preview.stdout
        assert ".github/instructions/child.instructions.md" in preview.stdout
    applied = runner.run(install, cwd=consumer.root, env=environment)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    lock = LockFile.read(consumer.root / "apm.lock.yaml")
    assert lock is not None
    if state == "required":
        assert {dep.repo_url for dep in lock.dependencies.values()} == {
            "_local/parent",
            "_local/child",
        }
        assert child_file.read_bytes() == child_bytes
        assert "Installed 2 APM dependencies" in applied.stdout
    else:
        assert not child_file.exists()
        assert not lock.dependencies


@pytest.mark.parametrize("intent", ["unchanged", "refresh", "frozen"])
def test_cached_remote_preview_preserves_resolution_intent(
    tmp_path: Path, apm_binary_path: Path, intent: str
) -> None:
    """Refresh and frozen mismatches cannot claim a complete cached preview."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer", dependencies=("owner/parent#v1",), targets=("copilot",)
    )
    installed = LocalPackageFactory(consumer.root / "apm_modules" / "owner")
    requested_commit = "b" * 40
    installed.create("parent", dependencies=(f"owner/child#{requested_commit}",))
    installed.create("child")
    lock = LockFile()
    lock.add_dependency(
        LockedDependency(repo_url="owner/parent", resolved_ref="v1", resolved_commit="a" * 40)
    )
    lock.add_dependency(
        LockedDependency(
            repo_url="owner/child",
            resolved_ref=requested_commit,
            resolved_commit="c" * 40 if intent == "frozen" else requested_commit,
        )
    )
    lock.write(consumer.root / "apm.lock.yaml")
    roots = {"project": isolated.work_root, "configuration": isolated.config_root}
    before = ArtifactSnapshotSet.capture(roots)
    flags = () if intent == "unchanged" else (f"--{intent}",)
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--dry-run", "--target", "copilot", "--no-policy", "--verbose", *flags),
        cwd=consumer.root,
        env=isolated.subprocess_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert ArtifactSnapshotSet.capture(roots) == before
    if intent == "unchanged":
        assert "APM dependencies (2)" in result.stdout
        assert "Dependency preview incomplete" not in result.stdout
    else:
        assert "Dependency preview incomplete" in result.stdout
        assert "Files that would be removed" not in result.stdout
        assert "Dry run completed:" not in result.stdout
        assert "locally known APM" in result.stdout
        if intent == "frozen":
            assert "incompatible immutable requirements" in result.stdout


@pytest.mark.parametrize("remote_child", [False, True])
def test_cold_transitive_native_preview_does_not_create_configuration(
    tmp_path: Path, apm_binary_path: Path, remote_child: bool
) -> None:
    """Both preflight validation routes keep absent user configuration absent."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    child_ref = "owner/native" if remote_child else "../native"
    factory.create("parent", dependencies=(child_ref,))
    consumer = factory.create("consumer", dependencies=("../parent",), targets=("copilot",))
    native = (
        consumer.root / "apm_modules" / "owner" / "native"
        if remote_child
        else isolated.work_root / "native"
    )
    write_agent_plugin(native, name="native", with_mcp=False)
    if remote_child:
        lock = LockFile()
        lock.add_dependency(LockedDependency(repo_url="owner/native", resolved_commit="a" * 40))
        lock.write(consumer.root / "apm.lock.yaml")
    roots = {"project-and-sources": isolated.work_root, "configuration": isolated.config_root}
    before = ArtifactSnapshotSet.capture(roots)
    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        ("install", "--dry-run", "--target", "copilot", "--no-policy"),
        cwd=consumer.root,
        env=isolated.subprocess_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "APM dependencies (2)" in result.stdout
    assert "Dependency preview incomplete" not in result.stdout
    assert ArtifactSnapshotSet.capture(roots) == before
    assert not (isolated.config_root / "config.json").exists()
