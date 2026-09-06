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
from tests.utils.lifecycle_state import LifecycleStateSnapshot
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
def remove(path, *args, **kwargs):
    blocked = os.environ.get("APM_TEST_BLOCKED_PACKAGE")
    if blocked and Path(path).resolve() == Path(blocked).resolve():
        if os.environ.get("APM_TEST_PARTIAL_DELETE") == "1":
            (Path(path) / "disposable.txt").unlink(missing_ok=True)
        raise PermissionError(errno.EACCES, "injected package deletion failure", str(path))
    return original_rmtree(path, *args, **kwargs)
shutil.rmtree = remove
cli()
"""


@pytest.mark.parametrize("partial_delete", [False, True], ids=["blocked", "partial"])
@pytest.mark.parametrize("package_count", [1, 2], ids=["single", "batch"])
@pytest.mark.parametrize("transitive", [False, True], ids=["direct", "transitive"])
def test_failed_package_deletion_retains_ownership_until_retry(
    tmp_path: Path, partial_delete: bool, package_count: int, transitive: bool
) -> None:
    """Installed -> failed removal -> retry keeps recoverable ownership, not rollback."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=os.environ)
    env = isolated.subprocess_env()
    # Keep the installed source CLI importable in the hermetic child process.
    env["PYTHONPATH"] = os.pathsep.join(
        [env["PYTHONPATH"], *(str(Path(path).resolve()) for path in sys.path if path)]
    )
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
    assert after.manifest_bytes == before.manifest_bytes
    assert after.lockfile_bytes == before.lockfile_bytes
    assert after.deployment_records == before.deployment_records
    assert after.files == before.files
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
