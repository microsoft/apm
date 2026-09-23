"""Real CLI cleanup outcomes under portable, selected OS deletion failures."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import requests

from apm_cli.cache.git_cache import GitCache
from apm_cli.cache.http_cache import HttpCache
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.lifecycle_smoke]

# Run the installed CLI entry point in a child interpreter so the only injected
# boundaries are unlink and its permission-repair chmod operation.
# Frozen binaries cannot load Python fault injection; no cache owner is mocked.
_CLI = """
import errno
import json
import os
from pathlib import Path
from apm_cli.cli import main

original_unlink = os.unlink
original_chmod = os.chmod
blocked_paths = [Path(path) for path in json.loads(os.environ.get("APM_TEST_DENIED_FILES", "[]"))]
blocked = {path.name for path in blocked_paths}
parents = {str(parent) for path in blocked_paths for parent in (path, *path.parents)}
def selected_unlink(path, *args, **kwargs):
    if os.path.basename(os.fsdecode(path)) in blocked:
        raise PermissionError(errno.EACCES, "selected cleanup denial", path)
    return original_unlink(path, *args, **kwargs)
def selected_chmod(path, *args, **kwargs):
    if os.fsdecode(path) in parents and args[0] == 0o200:
        raise PermissionError(errno.EACCES, "selected cleanup denial", path)
    return original_chmod(path, *args, **kwargs)
os.unlink = selected_unlink
os.chmod = selected_chmod
main()
"""


@pytest.mark.parametrize(
    "denied",
    [("db",), ("checkout",), ("http",), ("db", "http"), ()],
    ids=["db", "checkout", "http", "mixed", "control"],
)
def test_cache_clean_status_matches_residual_state(tmp_path: Path, denied: tuple[str, ...]) -> None:
    """Partial clean, retry, and repeated clean report the exact durable outcome."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    package = LocalPackageFactory(isolated.package_root).create("source")
    project = LocalPackageFactory(isolated.work_root).create("consumer")
    git_factory = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = git_factory.create("source", source_tree=package.root)
    git_factory.commit(repository, message="Seed cached package")
    cache = GitCache(isolated.cache_root)
    checkout = cache.get_checkout(repository.file_url, "main", env=environment)
    db = next((isolated.cache_root / "git" / "db_v1").iterdir())
    http_cache = HttpCache(isolated.cache_root)
    with LocalMcpRegistryFactory(tmp_path / "registry").start(
        {"name": "test/cache-clean", "description": "Cleanup fixture"}
    ) as registry:
        url = f"{registry.url}/v0.1/servers"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        http_cache.store(url, response.content, headers={"Cache-Control": "max-age=3600"})
        assert http_cache.get(url).body == response.content
    http = next((isolated.cache_root / "http_v1").iterdir())
    entries = {"db": db, "checkout": checkout, "http": http}
    for family, entry in entries.items():
        (entry / f"deny-{family}").write_bytes(family.encode("ascii"))
    (isolated.home / "unrelated.txt").write_bytes(b"preserve home\n")
    update_dir = "AppData/Local/apm/cache" if sys.platform == "win32" else ".cache/apm"
    update_cache = isolated.home / update_dir / "last_version_check"
    update_cache.parent.mkdir(parents=True)
    update_cache.touch()
    roots = {
        "cache": isolated.cache_root,
        "project": isolated.work_root,
        "source": isolated.package_root,
        "repositories": isolated.repository_root,
        "home": isolated.home,
    }
    before = ArtifactSnapshotSet.capture(roots)
    lifecycle_before = LifecycleStateSnapshot.capture(project.root)
    runner = ApmLifecycleRunner((sys.executable, "-c", _CLI))
    scenario = "cache-clean-" + ("-".join(denied) or "control")
    failed = runner.run(
        ("cache", "clean", "--force"),
        scenario_id=scenario,
        cwd=project.root,
        env={
            **environment,
            "APM_TEST_DENIED_FILES": json.dumps(
                [str(entries[family] / f"deny-{family}") for family in denied]
            ),
        },
    )
    after = ArtifactSnapshotSet.capture(roots)
    expected_paths = {"git", "git/db_v1", "git/checkouts_v1", "http_v1"}
    for family in denied:
        marker = (entries[family] / f"deny-{family}").relative_to(isolated.cache_root)
        expected_paths.add(marker.as_posix())
        expected_paths.update(parent.as_posix() for parent in marker.parents if parent != Path("."))
    residual = after.snapshot("cache")
    assert residual.paths == expected_paths
    original_entries = {entry.relative_path: entry for entry in before.snapshot("cache").entries}
    assert residual.entries == tuple(original_entries[path] for path in sorted(expected_paths))
    assert failed.returncode == (1 if denied else 0), failed.stdout + failed.stderr
    output = failed.stdout + failed.stderr
    if denied:
        assert "Cache cleanup incomplete" in output
        assert "Cache cleaned." not in output
        assert "retry" in output.lower()
        for family in denied:
            # The failed top-level shard is actionable even after its other
            # children were successfully removed.
            bucket = entries[family]
            while bucket.parent not in (
                isolated.cache_root / "git" / "db_v1",
                isolated.cache_root / "git" / "checkouts_v1",
                isolated.cache_root / "http_v1",
            ):
                bucket = bucket.parent
            assert str(bucket) in output.replace("\n", "")
    else:
        assert "Cache cleaned." in output
    for root in ("project", "source", "repositories", "home"):
        assert after.snapshot(root) == before.snapshot(root)
    assert LifecycleStateSnapshot.capture(project.root) == lifecycle_before

    retry = runner.run(
        ("cache", "clean", "--force"), scenario_id=scenario, cwd=project.root, env=environment
    )
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert "Cache cleaned." in retry.stdout
    converged = ArtifactSnapshotSet.capture(roots)
    assert converged.snapshot("cache").paths == {"git", "git/db_v1", "git/checkouts_v1", "http_v1"}
    repeat = runner.run(
        ("cache", "clean", "--force"), scenario_id=scenario, cwd=project.root, env=environment
    )
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert "Cache cleaned." in repeat.stdout
    assert ArtifactSnapshotSet.capture(roots) == converged
    for root in ("project", "source", "repositories", "home"):
        assert converged.snapshot(root) == before.snapshot(root)
    assert LifecycleStateSnapshot.capture(project.root) == lifecycle_before
