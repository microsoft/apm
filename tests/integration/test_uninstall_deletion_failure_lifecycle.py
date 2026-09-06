"""Real CLI transitions from installed through failed deletion to recovered removal."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateRoot, LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.windows_compat,
]

# Inject only the OS deletion failure, not command logic or persisted state.
_ENTRYPOINT = """
import errno
import os
import shutil
from pathlib import Path
from apm_cli.cli import cli

original_rmtree = shutil.rmtree
original_rename = Path.rename
def remove(path, *args, **kwargs):
    blocked = os.environ.get("APM_TEST_BLOCKED_PACKAGE")
    if blocked and Path(path).resolve() == Path(blocked).resolve():
        if os.environ.get("APM_TEST_PARTIAL_DELETE") == "1":
            (Path(path) / "disposable.txt").unlink(missing_ok=True)
        raise PermissionError(errno.EACCES, "injected package deletion failure", str(path))
    return original_rmtree(path, *args, **kwargs)
def rename(path, target):
    if (
        os.environ.get("APM_TEST_FAIL_REFRESH_RENAME") == "1"
        and path.name.startswith(".apm-uninstall-refresh-")
    ):
        raise PermissionError(errno.EACCES, "injected refresh activation failure", str(path))
    return original_rename(path, target)
shutil.rmtree = remove
Path.rename = rename
cli()
"""


def _child_env(isolated: IsolatedApmEnvironment) -> dict[str, str]:
    """Return a hermetic child environment that can import the source CLI."""
    env = isolated.subprocess_env()
    env["PYTHONPATH"] = os.pathsep.join(
        [env["PYTHONPATH"], *(str(Path(path).resolve()) for path in sys.path if path)]
    )
    return env


def _skill_text(name: str, marker: str) -> str:
    """Return one valid skill fixture with distinctive content."""
    return f"---\nname: {name}\ndescription: Recovery fixture\n---\n# {marker}\n"


def _instruction_text(marker: str) -> str:
    """Return one valid instruction fixture with distinctive content."""
    return f"---\napplyTo: '**'\ndescription: Recovery fixture\n---\n# {marker}\n"


def _assert_same_lifecycle_state(
    before: LifecycleStateSnapshot,
    after: LifecycleStateSnapshot,
) -> None:
    """Assert exact persisted ownership and deployed bytes are unchanged."""
    assert after.manifest_bytes == before.manifest_bytes
    assert after.lockfile_bytes == before.lockfile_bytes
    assert after.deployment_records == before.deployment_records
    assert after.files == before.files


def _assert_no_local_refresh_artifacts(local_root: Path) -> None:
    """Assert shared-slot staging and backup directories were retired."""
    assert not list(local_root.glob(".apm-uninstall-refresh-*"))
    assert not list(local_root.glob(".*.apm-uninstall-backup"))


@pytest.mark.parametrize("partial_delete", [False, True], ids=["blocked", "partial"])
@pytest.mark.parametrize("package_count", [1, 2], ids=["single", "batch"])
@pytest.mark.parametrize("transitive", [False, True], ids=["direct", "transitive"])
def test_failed_package_deletion_retains_ownership_until_retry(
    tmp_path: Path, partial_delete: bool, package_count: int, transitive: bool
) -> None:
    """Installed -> failed removal -> retry keeps recoverable ownership, not rollback."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=os.environ)
    env = _child_env(isolated)
    factory = LocalPackageFactory(isolated.work_root)
    owner = "apm-fixture-org" if transitive else "_local"
    packages = [
        factory.create(
            f"package-{index}",
            dependencies=("apm-fixture-org/transitive",)
            if transitive and index == package_count - 1
            else (),
        )
        for index in range(package_count)
    ]
    consumer = factory.create(
        "consumer",
        targets=("copilot",),
        dependencies=tuple(f"{owner}/{package.name}" for package in packages) if transitive else (),
    )
    if not transitive:
        for package in packages:
            factory.add_relative_dependency(consumer, package)
    blocked_source = packages[-1]
    if transitive:
        blocked_source = factory.create("transitive")
    sources = [*packages, *([blocked_source] if transitive else [])]
    for package in sources:
        (package.root / "payload.txt").write_bytes(b"owned package payload\n")
        (package.root / "disposable.txt").write_bytes(b"may be partially deleted\n")
        factory.add_skill(
            package,
            package.name,
            f"---\nname: {package.name}\ndescription: Recovery fixture\n---\n# Recovery\n",
        )
    if transitive:
        repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
        rewrites = []
        for package in sources:
            repository = repositories.create(package.name, source_tree=package.root)
            repositories.commit(repository, message=f"Seed {package.name}")
            rewrites.append((repository, f"https://github.com/{owner}/{package.name}"))
        env = repositories.url_rewrite_subprocess_env_many(rewrites)
    runner = ApmLifecycleRunner((sys.executable, "-c", _ENTRYPOINT))
    installed = runner.run(
        ("install", "--no-policy", "--parallel-downloads", "0"),
        scenario_id="deletion-failure-install",
        cwd=consumer.root,
        env=env,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    before = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    before_tree = ArtifactSnapshot.capture(consumer.root)
    assert before.lockfile_bytes is not None
    assert before.deployment_records
    materialized = [consumer.root / "apm_modules" / owner / package.name for package in packages]
    blocked = consumer.root / "apm_modules" / owner / blocked_source.name
    assert (blocked / "payload.txt").read_bytes() == b"owned package payload\n"
    args = ("uninstall", *(f"{owner}/{package.name}" for package in packages))
    failed = runner.run(
        args,
        scenario_id="deletion-failure-uninstall",
        cwd=consumer.root,
        env={
            **env,
            "APM_TEST_BLOCKED_PACKAGE": str(blocked),
            "APM_TEST_PARTIAL_DELETE": "1" if partial_delete else "0",
        },
    )
    output = failed.stdout + failed.stderr
    assert failed.returncode == 1, output
    assert "Uninstall complete" not in output
    assert "retry" in output
    after = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    _assert_same_lifecycle_state(before, after)
    assert (blocked / "payload.txt").read_bytes() == b"owned package payload\n"
    assert (blocked / "disposable.txt").exists() is not partial_delete
    if package_count == 1 and not partial_delete and not transitive:
        assert_unchanged(before_tree, ArtifactSnapshot.capture(consumer.root))
    if package_count == 2:
        assert not materialized[0].exists()

    recovered = runner.run(args, scenario_id="deletion-failure-retry", cwd=consumer.root, env=env)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert "Uninstall complete" in recovered.stdout
    final = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    assert not load_yaml(consumer.manifest_path)["dependencies"]["apm"]
    assert final.lockfile_bytes is None
    assert final.deployment_records == ()
    assert all(not path.exists() for path in materialized)
    assert not blocked.exists()


def test_global_failed_package_deletion_preserves_user_scope_until_retry(
    tmp_path: Path,
) -> None:
    """A global delete failure retains user ownership and names scoped recovery."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "global", base_env=os.environ)
    env = _child_env(isolated)
    factory = LocalPackageFactory(isolated.package_root)
    package = factory.create("global-package", targets=("copilot",))
    factory.add_skill(package, "global-skill", _skill_text("global-skill", "Global skill"))
    factory.add_instruction(package, "global", _instruction_text("Global instruction"))
    runner = ApmLifecycleRunner((sys.executable, "-c", _ENTRYPOINT))
    install = runner.run(
        (
            "install",
            "--global",
            str(package.root),
            "--target",
            "copilot",
            "--no-policy",
            "--parallel-downloads",
            "0",
        ),
        scenario_id="global-deletion-failure-install",
        cwd=isolated.work_root,
        env=env,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    materialized = isolated.config_root / "apm_modules" / "_local" / package.name
    external_roots = (
        LifecycleStateRoot(
            root_id="copilot-user",
            target="copilot",
            scope="user",
            path=isolated.home / ".copilot",
        ),
    )
    before = LifecycleStateSnapshot.capture(
        isolated.config_root,
        external_roots=external_roots,
    )
    materialized_before = ArtifactSnapshot.capture(materialized)
    project_before = ArtifactSnapshot.capture(isolated.work_root)
    args = ("uninstall", "--global", str(package.root))

    failed = runner.run(
        args,
        scenario_id="global-deletion-failure-uninstall",
        cwd=isolated.work_root,
        env={**env, "APM_TEST_BLOCKED_PACKAGE": str(materialized)},
    )
    output = failed.stdout + failed.stderr
    assert failed.returncode == 1, output
    assert "Uninstall complete" not in output
    assert "apm install --global" in " ".join(output.split())
    assert str(package.root) not in output
    after = LifecycleStateSnapshot.capture(
        isolated.config_root,
        external_roots=external_roots,
    )
    _assert_same_lifecycle_state(before, after)
    assert_unchanged(materialized_before, ArtifactSnapshot.capture(materialized))
    assert_unchanged(project_before, ArtifactSnapshot.capture(isolated.work_root))

    recovered = runner.run(
        args,
        scenario_id="global-deletion-failure-retry",
        cwd=isolated.work_root,
        env=env,
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert "Uninstall complete" in recovered.stdout
    assert str(package.root) not in recovered.stdout + recovered.stderr
    assert not (isolated.home / ".copilot").exists()
    final = LifecycleStateSnapshot.capture(isolated.config_root)
    assert not load_yaml(isolated.config_root / "apm.yml")["dependencies"]["apm"]
    assert final.lockfile_bytes is None
    assert final.deployment_records == ()
    assert not materialized.exists()
    assert_unchanged(project_before, ArtifactSnapshot.capture(isolated.work_root))


def test_shared_local_slot_rename_failure_restores_original_until_retry(
    tmp_path: Path,
) -> None:
    """Failed survivor activation restores the original shared slot atomically."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "shared-slot", base_env=os.environ)
    env = _child_env(isolated)
    first_factory = LocalPackageFactory(isolated.root / "source-one")
    second_factory = LocalPackageFactory(isolated.root / "source-two")
    first = first_factory.create("Shared Package", targets=("copilot",))
    second = second_factory.create("Shared Package", targets=("copilot",))
    first_factory.add_skill(first, "first", _skill_text("first", "First survivor"))
    second_factory.add_skill(second, "second", _skill_text("second", "Second declaration"))
    project_factory = LocalPackageFactory(isolated.work_root)
    project = project_factory.create("consumer", targets=("copilot",))
    first_path = Path(os.path.relpath(first.root, project.root)).as_posix()
    second_path = Path(os.path.relpath(second.root, project.root)).as_posix()
    project_factory.replace_apm_dependencies(project, (first_path, second_path))
    runner = ApmLifecycleRunner((sys.executable, "-c", _ENTRYPOINT))
    install = runner.run(
        ("install", "--target", "copilot", "--no-policy", "--parallel-downloads", "0"),
        scenario_id="shared-slot-install",
        cwd=project.root,
        env=env,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    local_root = project.root / "apm_modules" / "_local"
    materialized = local_root / first.name
    original_materialized = ArtifactSnapshot.capture(materialized)
    before = LifecycleStateSnapshot.capture(project.root, targets=("copilot",))

    failed = runner.run(
        ("uninstall", second_path),
        scenario_id="shared-slot-rename-failure",
        cwd=project.root,
        env={**env, "APM_TEST_FAIL_REFRESH_RENAME": "1"},
    )
    output = failed.stdout + failed.stderr
    assert failed.returncode != 0, output
    assert "Uninstall complete" not in output
    after = LifecycleStateSnapshot.capture(project.root, targets=("copilot",))
    _assert_same_lifecycle_state(before, after)
    assert_unchanged(original_materialized, ArtifactSnapshot.capture(materialized))
    _assert_no_local_refresh_artifacts(local_root)

    recovered = runner.run(
        ("uninstall", second_path),
        scenario_id="shared-slot-rename-retry",
        cwd=project.root,
        env=env,
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert load_yaml(project.manifest_path)["dependencies"]["apm"] == [first_path]
    lock = load_yaml(project.root / "apm.lock.yaml")
    local_entries = [
        entry
        for entry in lock["dependencies"]
        if isinstance(entry, dict) and entry.get("source") == "local"
    ]
    assert len(local_entries) == 1
    assert local_entries[0]["local_path"] == first_path
    assert (materialized / "skills/first/SKILL.md").is_file()
    assert not (materialized / "skills/second").exists()
    assert (project.root / ".agents/skills/first/SKILL.md").is_file()
    assert not (project.root / ".agents/skills/second").exists()
    _assert_no_local_refresh_artifacts(local_root)


def test_user_edited_target_refusal_retains_ownership_after_package_deletion(
    tmp_path: Path,
) -> None:
    """A refused target cleanup remains owned after package deletion and retries."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "target-refusal", base_env=os.environ)
    env = _child_env(isolated)
    factory = LocalPackageFactory(isolated.work_root)
    package = factory.create("source-package", targets=("copilot",))
    factory.add_skill(package, "owned-skill", _skill_text("owned-skill", "Managed bytes"))
    consumer = factory.create("consumer", targets=("copilot",))
    factory.add_relative_dependency(consumer, package)
    declared_path = Path(os.path.relpath(package.root, consumer.root)).as_posix()
    runner = ApmLifecycleRunner((sys.executable, "-c", _ENTRYPOINT))
    install = runner.run(
        ("install", "--target", "copilot", "--no-policy", "--parallel-downloads", "0"),
        scenario_id="target-refusal-install",
        cwd=consumer.root,
        env=env,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    materialized = consumer.root / "apm_modules" / "_local" / package.name
    target = consumer.root / ".agents" / "skills" / "owned-skill" / "SKILL.md"
    managed_bytes = target.read_bytes()
    user_bytes = managed_bytes + b"\n# User edit\n"
    target.write_bytes(user_bytes)
    before = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))

    failed = runner.run(
        ("uninstall", declared_path),
        scenario_id="target-refusal-uninstall",
        cwd=consumer.root,
        env=env,
    )
    output = failed.stdout + failed.stderr
    assert failed.returncode != 0, output
    assert "Uninstall complete" not in output
    assert not materialized.exists()
    after = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    _assert_same_lifecycle_state(before, after)
    assert target.read_bytes() == user_bytes

    target.write_bytes(managed_bytes)
    recovered = runner.run(
        ("uninstall", declared_path),
        scenario_id="target-refusal-retry",
        cwd=consumer.root,
        env=env,
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert "Uninstall complete" in recovered.stdout
    final = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    assert not load_yaml(consumer.manifest_path)["dependencies"]["apm"]
    assert final.lockfile_bytes is None
    assert final.deployment_records == ()
    assert not target.exists()
