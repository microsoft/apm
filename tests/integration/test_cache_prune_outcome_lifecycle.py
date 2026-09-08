"""Real CLI engine contract for cache pruning failures and recovery."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.cache.git_cache import GitCache
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactDiff, ArtifactSnapshot, ArtifactSnapshotSet
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
]

# Extend the isolated environment's startup guard, leaving IP networking
# disabled. Only the selected rmtree boundary is faulted, not the CLI.
_REMOVAL_FAULT = """
import errno
import shutil
from pathlib import Path

original_rmtree = shutil.rmtree

def remove(path, *args, **kwargs):
    blocked = Path(os.environ["PRUNE_BLOCKED_PATH"])
    if Path(path) == blocked:
        if os.environ["PRUNE_PARTIAL"] == "1":
            (blocked / "full" / "partial.txt").unlink(missing_ok=True)
        raise PermissionError(errno.EACCES, "fixture removal denied", str(blocked))
    return original_rmtree(path, *args, **kwargs)

if os.environ.get("PRUNE_BLOCKED_PATH"):
    shutil.rmtree = remove
"""


@pytest.mark.parametrize("partial_removal", [False, True])
def test_prune_failure_retry_and_convergence(
    tmp_path: Path, apm_engine_command: tuple[str, ...], partial_removal: bool
) -> None:
    """Counts, status and complete survivor trees agree at every transition."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    GitCache(isolated.cache_root)
    environment = isolated.subprocess_env()
    environment["APM_E2E_TESTS"] = "1"
    environment["COLUMNS"] = "240"
    guard = isolated.root / "network_guard" / "sitecustomize.py"
    guard.write_text(guard.read_text(encoding="utf-8") + _REMOVAL_FAULT, encoding="utf-8")
    checkouts = isolated.cache_root / "git" / "checkouts_v1" / "fixture"
    blocked = checkouts / ("b" * 40)
    stale = checkouts / ("a" * 40)
    recent = checkouts / ("c" * 40)
    for checkout in (blocked, stale, recent):
        (checkout / "full").mkdir(parents=True)
        (checkout / "full" / "keep.txt").write_bytes(b"checkout payload\n")
        (checkout / "full" / "partial.txt").write_bytes(b"partially removable\n")
        timestamp = 4102444800 if checkout == recent else 1
        os.utime(checkout, (timestamp, timestamp))
    for root in (
        isolated.work_root,
        isolated.package_root,
        isolated.home,
        isolated.cache_root / "git" / "db_v1",
    ):
        (root / "sentinel.txt").write_bytes(b"unrelated durable state\n")
    (isolated.cache_root / "http").mkdir()
    (isolated.cache_root / "http" / "response.json").write_bytes(b'{"cached": true}\n')
    roots = {
        "project": isolated.work_root,
        "source": isolated.package_root,
        "home": isolated.home,
    }
    protected = ArtifactSnapshotSet.capture(roots)
    before = ArtifactSnapshot.capture(isolated.cache_root)
    stale_prefix = stale.relative_to(isolated.cache_root).as_posix()
    blocked_prefix = blocked.relative_to(isolated.cache_root).as_posix()
    stale_paths = frozenset(
        path for path in before.paths if path == stale_prefix or path.startswith(stale_prefix + "/")
    )
    partial_paths = (
        frozenset({blocked_prefix + "/full/partial.txt"}) if partial_removal else frozenset()
    )
    environment.update(
        PRUNE_BLOCKED_PATH=str(blocked),
        PRUNE_PARTIAL="1" if partial_removal else "0",
    )
    runner = ApmLifecycleRunner(apm_engine_command)

    for attempt, count in (("mixed", 1), ("blocked-only", 0)):
        result = runner.run(
            ("cache", "prune", "--days", "30"),
            scenario_id=f"cache-prune-{attempt}-partial-{partial_removal}",
            cwd=isolated.work_root,
            env=environment,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        assert f"Pruned {count} SHA group(s); 1 failed." in output
        assert "fixture removal denied" in output
        assert "Check permissions or close programs using the cache, then retry." in output
        assert blocked.name in output
        assert "[+]" not in output
        assert "Traceback" not in output
        after = ArtifactSnapshot.capture(isolated.cache_root)
        assert before.diff(after) == ArtifactDiff(
            added=frozenset(),
            removed=stale_paths | partial_paths,
            changed=frozenset(),
        )
        assert ArtifactSnapshotSet.capture(roots) == protected

    failed_state = ArtifactSnapshot.capture(isolated.cache_root)
    blocked_paths = frozenset(
        path
        for path in failed_state.paths
        if path == blocked_prefix or path.startswith(blocked_prefix + "/")
    )
    environment.pop("PRUNE_BLOCKED_PATH")
    for attempt, count in (("retry", 1), ("converged", 0)):
        result = runner.run(
            ("cache", "prune", "--days", "30"),
            scenario_id=f"cache-prune-{attempt}-partial-{partial_removal}",
            cwd=isolated.work_root,
            env=environment,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert f"Pruned {count} SHA group(s)." in output
        assert "[x]" not in output
        assert "[!]" not in output
        assert failed_state.diff(ArtifactSnapshot.capture(isolated.cache_root)) == ArtifactDiff(
            added=frozenset(), removed=blocked_paths, changed=frozenset()
        )
        assert ArtifactSnapshotSet.capture(roots) == protected
