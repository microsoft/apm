"""Real install/cache-prune lifecycle contract for nonnegative cache ages."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]


def test_negative_prune_preserves_installed_cache_and_valid_ages(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env(overrides={"APM_TIERED_RESOLVER": "1"})
    packages = LocalPackageFactory(isolated.package_root)
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    remotes = []
    for name in ("prune-age-a", "prune-age-b"):
        source = packages.create(name, targets=("copilot",))
        packages.add_skill(
            source,
            name,
            f"---\nname: {name}\ndescription: Cache age fixture\n---\n# {name}\n",
        )
        repository = repositories.create(name, source_tree=source.root)
        repositories.commit(repository, message=f"Publish {name}")
        remotes.append((repository, f"https://github.com/apm-fixture-org/{name}"))
    environment = repositories.url_rewrite_subprocess_env_many(tuple(remotes))
    consumer = LocalPackageFactory(isolated.work_root).create(
        "prune-age-consumer",
        dependencies=tuple({"git": url, "ref": "main"} for _, url in remotes),
        targets=("copilot",),
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=180)

    def run(*args: str) -> CommandResult:
        return runner.run(
            args,
            scenario_id="cache-prune-age",
            cwd=consumer.root,
            env=environment,
        )

    installed = run("install", "--target", "copilot", "--no-policy", "--parallel-downloads", "0")
    assert installed.returncode == 0, installed
    state = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    assert state.lockfile_bytes
    assert state.deployment_records
    checkout_root = isolated.cache_root / "git" / "checkouts_v1"
    checkouts = sorted(path for path in checkout_root.glob("*/*") if path.is_dir())
    assert len(checkouts) == 2
    roots = {
        "cache": isolated.cache_root,
        "project": consumer.root,
        "home": isolated.home,
        "sources": isolated.package_root,
        "repositories": isolated.repository_root,
    }
    before = ArtifactSnapshotSet.capture(roots)
    cache_metadata = {
        path: (path.lstat().st_mode, path.lstat().st_mtime_ns)
        for path in (isolated.cache_root, *isolated.cache_root.rglob("*"))
    }
    rejected = run("cache", "prune", "--days", "-1")
    after = ArtifactSnapshotSet.capture(roots)
    for root_id in roots:
        assert_unchanged(before.snapshot(root_id), after.snapshot(root_id))
    assert cache_metadata == {
        path: (path.lstat().st_mode, path.lstat().st_mtime_ns)
        for path in (isolated.cache_root, *isolated.cache_root.rglob("*"))
    }
    assert rejected.returncode == 2, rejected
    assert "--days" in rejected.stderr
    assert "Pruned" not in rejected.stdout
    assert LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",)) == state

    for args in (("cache", "prune"), ("cache", "prune", "--days", "30")):
        result = run(*args)
        assert result.returncode == 0, result
        assert_unchanged(
            before.snapshot("cache"), ArtifactSnapshotSet.capture(roots).snapshot("cache")
        )

    old, fresh = checkouts
    old_time = time.time() - 60 * 86400
    os.utime(old, (old_time, old_time))
    positive = run("cache", "prune", "--days", "30")
    assert positive.returncode == 0, positive
    assert not old.exists()
    assert fresh.is_dir()

    past = time.time() - 60
    os.utime(fresh, (past, past))
    zero = run("cache", "prune", "--days", "0")
    assert zero.returncode == 0, zero
    assert not fresh.exists()
    final = ArtifactSnapshotSet.capture(roots)
    for root_id in ("project", "home", "sources", "repositories"):
        assert_unchanged(before.snapshot(root_id), final.snapshot(root_id))
    assert LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",)) == state
