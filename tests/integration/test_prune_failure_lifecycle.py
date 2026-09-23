"""Real Python CLI subprocess contracts for orphan deletion failures.

The interpreter entry point permits an OS audit-hook fault without adding a
production test switch or replacing prune/cleanup logic. Frozen binary coverage
for ordinary pruning lives in test_prune_deployment_ledger_e2e.py.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import (
    ArtifactSnapshot,
    assert_only_paths_changed,
    assert_unchanged,
)
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.lifecycle_smoke]

_FAULT_ENTRY = """\
import errno
import json
import sys
from pathlib import Path

sys.path.extend(json.loads(sys.argv.pop(1)))
blocked = Path(sys.argv.pop(1)).resolve()
mode = sys.argv.pop(1)

def deny_selected_removal(event, args):
    if event != "shutil.rmtree" or Path(args[0]).resolve() != blocked:
        return
    if mode == "partial":
        # Model a removal that already committed one leaf before EACCES.
        (blocked / "payload.txt").unlink()
    raise PermissionError(errno.EACCES, "injected orphan deletion denial", str(blocked))

if mode != "none":
    sys.addaudithook(deny_selected_removal)
from apm_cli.cli import main
main()
"""


def _runner(blocked: Path, mode: str) -> ApmLifecycleRunner:
    """Use the installed CLI with import roots preserved despite isolated HOME."""
    import_roots = json.dumps([str(Path(path).resolve()) for path in sys.path if path])
    return ApmLifecycleRunner(
        (sys.executable, "-c", _FAULT_ENTRY, import_roots, str(blocked), mode)
    )


@pytest.mark.parametrize("mode", ["blocked", "mixed", "partial"])
def test_prune_failure_reports_partial_state_and_retry_converges(tmp_path: Path, mode: str) -> None:
    """Failure status tracks actual removals; retry preserves all unowned bytes."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    environment = isolated.subprocess_env()
    consumer = LocalPackageFactory(isolated.work_root).create("consumer")
    modules = consumer.root / "apm_modules"
    packages = LocalPackageFactory(modules / "orphan-org")
    blocked = packages.create("blocked")
    payload = blocked.root / "payload.txt"
    payload.write_bytes(b"orphan payload\n")
    removed_count = 2 if mode == "mixed" else 0
    if mode == "mixed":
        packages.create("after")
        packages.create("z-last")
    sentinel = modules / "user-notes.txt"
    sentinel.write_bytes(b"unowned module-root note\n")
    (consumer.root / "user-notes.txt").write_bytes(b"unowned project note\n")
    before = ArtifactSnapshot.capture(consumer.root)
    state = LifecycleStateSnapshot.capture(consumer.root)
    runner = _runner(blocked.root, mode)

    failed = runner.run(
        ("prune",), cwd=consumer.root, env=environment, scenario_id=f"prune-{mode}-denied"
    )
    output = " ".join((failed.stdout + failed.stderr).split())
    assert "Failed to remove orphan-org/blocked" in output
    assert "injected orphan deletion denial" in output
    assert failed.returncode == 1, output
    assert (
        f"Prune incomplete: removed {removed_count} orphaned package(s); "
        "failed to remove 1 package(s)."
    ) in output
    assert "rerun 'apm prune'" in output
    assert "No packages were removed" not in output
    assert "Pruned " not in output
    after_failure = ArtifactSnapshot.capture(consumer.root)
    difference = before.diff(after_failure)
    expected_removed = set()
    if mode == "mixed":
        expected_removed.update(
            f"apm_modules/orphan-org/{name}{suffix}"
            for name in ("after", "z-last")
            for suffix in ("", "/apm.yml")
        )
    elif mode == "partial":
        expected_removed.add("apm_modules/orphan-org/blocked/payload.txt")
    assert difference.removed == expected_removed
    assert difference.added == difference.changed == frozenset()
    assert LifecycleStateSnapshot.capture(consumer.root) == state
    assert blocked.manifest_path.is_file()

    retry_runner = _runner(blocked.root, "none")
    retry, repeat = retry_runner.run_sequence(
        (("prune",), ("prune", "--dry-run")),
        expected_returncodes=(0, 0),
        scenario_id=f"prune-{mode}-retry",
        cwd=consumer.root,
        env=environment,
    )
    assert "Pruned 1 orphaned package(s)" in retry.stdout
    assert "No orphaned packages found" in repeat.stdout
    recovered = ArtifactSnapshot.capture(consumer.root)
    assert not (modules / "orphan-org").exists()
    assert recovered.paths == {
        "apm.yml",
        "apm_modules",
        "apm_modules/user-notes.txt",
        "user-notes.txt",
    }
    assert before.diff(recovered).added == before.diff(recovered).changed == frozenset()
    assert LifecycleStateSnapshot.capture(consumer.root) == state

    repeated = retry_runner.run(
        ("prune",), cwd=consumer.root, env=environment, scenario_id=f"prune-{mode}-repeat"
    )
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert "No orphaned packages found" in repeated.stdout
    assert_unchanged(recovered, ArtifactSnapshot.capture(consumer.root))
    assert LifecycleStateSnapshot.capture(consumer.root) == state


def test_prune_mixed_failure_reconciles_only_successful_owners(tmp_path: Path) -> None:
    """A real install's failed owner survives while successful owners are pruned."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    environment = isolated.subprocess_env()
    factory = LocalPackageFactory(isolated.work_root)
    consumer = factory.create("consumer", targets=("copilot",))
    for name in ("after", "blocked", "z-last"):
        source = factory.create(name, targets=("copilot",))
        factory.add_instruction(source, name, f"---\napplyTo: '**'\n---\n# {name} instruction\n")
        factory.add_relative_dependency(consumer, source)
    blocked = consumer.root / "apm_modules/_local/blocked"
    healthy_runner = _runner(blocked, "none")
    installed = healthy_runner.run(
        ("install", "--target", "copilot", "--no-policy"),
        cwd=consumer.root,
        env=environment,
        scenario_id="prune-mixed-install",
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"]["apm"] = []
    dump_yaml(manifest, consumer.manifest_path)
    sentinel = consumer.root / ".github/instructions/manual.instructions.md"
    sentinel.write_bytes(b"# Unowned instruction\n")
    before = ArtifactSnapshot.capture(consumer.root)
    state = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    blocked_records = tuple(
        record
        for record in state.deployment_records
        if record.locator.value == ".github/instructions/blocked.instructions.md"
    )
    assert len(state.deployment_records) == 3
    assert len(blocked_records) == 1

    failed = _runner(blocked, "blocked").run(
        ("prune",), cwd=consumer.root, env=environment, scenario_id="prune-mixed-owned-denial"
    )
    output = " ".join((failed.stdout + failed.stderr).split())
    assert failed.returncode == 1, output
    assert (
        "Prune incomplete: removed 2 orphaned package(s); failed to remove 1 package(s)." in output
    )
    after = ArtifactSnapshot.capture(consumer.root)
    retained = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    lock = LockFile.read(consumer.root / "apm.lock.yaml")
    assert lock is not None
    assert set(lock.dependencies) == set(blocked_records[0].owners)
    assert retained.deployment_records == blocked_records
    assert retained.manifest_bytes == state.manifest_bytes
    assert (
        retained.file(".github/instructions/blocked.instructions.md").content
        == state.file(".github/instructions/blocked.instructions.md").content
    )
    successful_paths = {
        path
        for path in before.paths
        if any(
            path == f"apm_modules/_local/{name}"
            or path.startswith(f"apm_modules/_local/{name}/")
            or path == f".github/instructions/{name}.instructions.md"
            for name in ("after", "z-last")
        )
    }
    assert before.diff(after).removed == successful_paths
    assert_only_paths_changed(before, after, successful_paths | {"apm.lock.yaml"})

    retry = healthy_runner.run(
        ("prune",), cwd=consumer.root, env=environment, scenario_id="prune-mixed-owned-retry"
    )
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert "Pruned 1 orphaned package(s)" in retry.stdout
    recovered = ArtifactSnapshot.capture(consumer.root)
    recovered_state = LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",))
    assert recovered_state.deployment_records == ()
    assert recovered_state.lockfile_bytes is None
    assert recovered_state.manifest_bytes == state.manifest_bytes
    assert not (consumer.root / "apm_modules/_local").exists()
    assert sentinel.read_bytes() == b"# Unowned instruction\n"
    allowed_retry_paths = {
        path
        for path in after.paths
        if path == "apm_modules/_local"
        or path.startswith("apm_modules/_local/blocked")
        or path in {"apm.lock.yaml", ".github/instructions/blocked.instructions.md"}
    }
    assert_only_paths_changed(after, recovered, allowed_retry_paths)
    repeated = healthy_runner.run(
        ("prune",), cwd=consumer.root, env=environment, scenario_id="prune-mixed-owned-repeat"
    )
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert_unchanged(recovered, ArtifactSnapshot.capture(consumer.root))
    assert LifecycleStateSnapshot.capture(consumer.root, targets=("copilot",)) == recovered_state
