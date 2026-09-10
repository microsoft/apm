"""Real local processes and independent exact-byte assessment workspaces."""

import hashlib
import json
import os
import signal
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from apm_cli.contracts import process, records, workspace
from apm_cli.contracts.models import (
    CheckObservation,
    ContractError,
    ContractLimits,
    LeafContract,
    LeafPlan,
    Outcome,
    ProcessObservation,
    ProcessRequest,
    RunResult,
)
from apm_cli.utils.atomic_io import atomic_write_text

pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(os.name != "posix", reason="Native contract execution is POSIX-only"),
]


def _plan(root: Path) -> LeafPlan:
    source = root / "test.contract.md"
    source.write_bytes(b"---\nproduces: fix.patch\n---\nWrite a patch.\n")
    (root / "apm.yml").write_text("name: fixture\nversion: 0.0.0\n", encoding="utf-8")
    (root / "input.txt").write_bytes(b"old\r\n")
    return LeafPlan(
        LeafContract(
            source,
            hashlib.sha256(source.read_bytes()).hexdigest(),
            "Patch",
            ("input.txt",),
            "fix.patch",
            (),
        ),
        root,
        Path(sys.executable),
    )


def _capture(tmp_path: Path) -> tuple[LeafPlan, Path]:
    plan = _plan(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    return plan, run


def test_exact_bytes_and_independent_self_applied_patch(tmp_path: Path) -> None:
    plan, run = _capture(tmp_path)
    snapshot = workspace.capture_workspace(plan, run)
    assert (snapshot.root / "input.txt").read_bytes() == b"old\r\n"
    assert workspace.local_git(snapshot.root, "show", "HEAD:input.txt") == b"old\r\n"
    patch = b"diff --git a/input.txt b/input.txt\n--- a/input.txt\n+++ b/input.txt\n@@ -1 +1 @@\n-old\r\n+new\r\n"
    (snapshot.producer / "fix.patch").write_bytes(patch)
    artifact = workspace.capture_output(snapshot, "fix.patch", run, plan.limits)
    assert artifact is not None
    assert artifact.sha256 == hashlib.sha256(patch).hexdigest()
    for name in ("one", "two"):
        check = workspace.prepare_check_workspace(snapshot, artifact, run, name)
        assert (check / "input.txt").read_bytes() == b"old\r\n"
        workspace.local_git(check, "apply", "--", "fix.patch")
        assert (check / "input.txt").read_bytes() == b"new\r\n"
        assert workspace.verify_check_integrity(snapshot, artifact, check)
    assert (tmp_path / "input.txt").read_bytes() == b"old\r\n"


def test_effective_tracked_edits_deletions_and_selected_untracked(tmp_path: Path) -> None:
    plan, run = _capture(tmp_path)
    workspace.local_git(tmp_path, "init", "--quiet")
    (tmp_path / "deleted.txt").write_text("remove", encoding="utf-8")
    workspace.local_git(tmp_path, "add", "--", "input.txt", "deleted.txt")
    workspace.local_git(tmp_path, "commit", "--quiet", "-m", "Original")
    original = workspace.local_git(tmp_path, "rev-parse", "HEAD").decode().strip()
    (tmp_path / "input.txt").write_bytes(b"working bytes\n")
    (tmp_path / "deleted.txt").unlink()
    (tmp_path / "unselected-secret.txt").write_text("not selected", encoding="utf-8")
    snapshot = workspace.capture_workspace(plan, run)
    assert snapshot.original_head == original
    assert workspace.local_git(snapshot.root, "show", "HEAD:input.txt") == b"working bytes\n"
    assert not (snapshot.root / "deleted.txt").exists()
    assert not (snapshot.root / "unselected-secret.txt").exists()
    assert (snapshot.root / "test.contract.md").exists()
    assert workspace.local_git(snapshot.root, "remote") == b""


def test_inventory_never_executes_configured_fsmonitor(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    workspace.local_git(tmp_path, "init", "--quiet")
    workspace.local_git(tmp_path, "add", "--", "input.txt")
    hook = tmp_path / "fsmonitor-hook"
    marker = tmp_path / "hook-executed"
    hook.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    workspace.local_git(tmp_path, "config", "core.fsmonitor", str(hook))
    entries = workspace.inspect_workspace(plan)
    assert any(entry.relative_path == "input.txt" for entry in entries)
    assert not marker.exists()


def test_missing_empty_oversized_and_symlink_outputs_are_distinct(tmp_path: Path) -> None:
    plan, run = _capture(tmp_path)
    snapshot = workspace.capture_workspace(plan, run)
    assert workspace.capture_output(snapshot, "fix.patch", run, plan.limits) is None
    output = snapshot.producer / "fix.patch"
    output.symlink_to(tmp_path / "input.txt")
    with pytest.raises(ContractError):
        workspace.capture_output(snapshot, "fix.patch", run, plan.limits)
    output.unlink()
    output.write_bytes(b"too large")
    with pytest.raises(ContractError):
        workspace.capture_output(snapshot, "fix.patch", run, replace(plan.limits, output_bytes=1))
    output.write_bytes(b"")
    artifact = workspace.capture_output(snapshot, "fix.patch", run, plan.limits)
    assert artifact is not None
    assert artifact.size == 0
    assert artifact.sha256 == hashlib.sha256(b"").hexdigest()


def test_changed_check_resource_is_incomplete(tmp_path: Path) -> None:
    plan, run = _capture(tmp_path)
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "condition.txt").write_bytes(b"authoritative")
    snapshot = workspace.capture_workspace(plan, run)
    (snapshot.producer / "fix.patch").write_bytes(b"candidate")
    artifact = workspace.capture_output(snapshot, "fix.patch", run, plan.limits)
    assert artifact is not None
    root = workspace.prepare_check_workspace(snapshot, artifact, run, "one")
    assert workspace.verify_check_integrity(snapshot, artifact, root)
    (root / "checks" / "condition.txt").write_bytes(b"changed")
    assert not workspace.verify_check_integrity(snapshot, artifact, root)
    assert records.normalize_check(ProcessObservation(0), integrity_ok=False) == 2


@pytest.mark.parametrize(
    ("raw", "expected"), [(0, 0), (1, 1), (2, 2), (127, 2), (-9, 2), (None, 2)]
)
def test_raw_check_normalization(raw: int | None, expected: int) -> None:
    assert records.normalize_check(ProcessObservation(raw)) == expected
    assert records.normalize_check(ProcessObservation(raw, cleanup_confirmed=False)) == 2


def test_empty_checks_and_failure_plus_incomplete_outcomes() -> None:
    incomplete = CheckObservation("missing", "", ProcessObservation(127), 2, "", "", "missing")
    failed = replace(incomplete, name="failed", normalized=1, process=ProcessObservation(1))
    assert records.reduce_outcome(None, (), None) == Outcome.UNPROVEN
    assert records.reduce_outcome(None, (incomplete, failed), None) == Outcome.REJECTED
    assert records.reduce_outcome(None, (failed,), "cancelled") == Outcome.HALTED


def test_both_streams_are_drained_and_spawn_failure_is_observed(tmp_path: Path) -> None:
    chunks: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    observation = process.supervise_process(
        ProcessRequest(
            (sys.executable, "-c", "import os; os.write(1,b'a'*200000); os.write(2,b'b'*200000)"),
            tmp_path,
            10,
        ),
        on_bytes=lambda stream, data: chunks[stream].extend(data),
    )
    assert observation.returncode == 0
    assert observation.cleanup_confirmed
    assert chunks == {"stdout": b"a" * 200000, "stderr": b"b" * 200000}
    missing = process.supervise_process(
        ProcessRequest((str(tmp_path / "no-executable"),), tmp_path, 1),
        on_bytes=lambda *args: None,
    )
    assert missing.returncode is None
    assert missing.error


def test_watchdog_kills_term_ignoring_child_with_bounded_cleanup(tmp_path: Path) -> None:
    started = time.monotonic()
    observation = process.supervise_process(
        ProcessRequest(
            (
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)",
            ),
            tmp_path,
            0.2,
        ),
        on_bytes=lambda *args: None,
        limits=ContractLimits(cleanup_seconds=0.4),
    )
    assert observation.stop_reason == "timeout"
    assert observation.cleanup_confirmed
    assert observation.returncode == -signal.SIGKILL
    assert time.monotonic() - started < 2
    assert observation.signals == ("SIGTERM", "SIGKILL")


def test_natural_child_shutdown_is_not_an_operational_failure(tmp_path: Path) -> None:
    chunks = bytearray()
    code = (
        "import subprocess,sys\n"
        "subprocess.Popen([sys.executable,'-c',"
        "'import time; time.sleep(0.15); print(\"closed\",flush=True)'])\n"
    )
    observation = process.supervise_process(
        ProcessRequest((sys.executable, "-c", code), tmp_path, 10),
        on_bytes=lambda stream, data: chunks.extend(data),
    )
    assert observation.returncode == 0
    assert observation.stop_reason is None
    assert observation.cleanup_confirmed
    assert observation.signals == ()
    assert b"closed" in chunks


def test_truly_lingering_child_still_halts_within_cleanup_window(tmp_path: Path) -> None:
    started = time.monotonic()
    observation = process.supervise_process(
        ProcessRequest(
            (
                sys.executable,
                "-c",
                "import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(30)'])",
            ),
            tmp_path,
            10,
        ),
        on_bytes=lambda *args: None,
        limits=ContractLimits(cleanup_seconds=0.4),
    )
    assert observation.returncode == 0
    assert observation.stop_reason == "lingering_children"
    assert "SIGTERM" in observation.signals
    assert time.monotonic() - started < 2
    assert records.reduce_outcome(None, (), observation.stop_reason) == Outcome.HALTED


def test_callback_failure_still_reaps_the_child(tmp_path: Path) -> None:
    pids = []

    def fail(stream: str, data: bytes) -> None:
        raise OSError("transcript storage failed")

    with pytest.raises(OSError, match="transcript storage failed"):
        process.supervise_process(
            ProcessRequest(
                (sys.executable, "-c", "import time; print('ready',flush=True); time.sleep(30)"),
                tmp_path,
                10,
            ),
            on_bytes=fail,
            on_started=lambda pid, pgid: pids.append(pid),
        )
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


def test_record_is_private_atomic_and_observed_not_claimed(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    store = records.AttemptStore.create(plan)
    store.update("execution", producer=ProcessObservation(7), observed_models=("gpt-6-astra",))
    data = json.loads(store.record_path.read_text())
    assert data["complete"] is False
    assert data["producer"]["returncode"] == 7
    assert data["observed_models"] == ["gpt-6-astra"]
    assert store.record_path.stat().st_mode & 0o777 == 0o600
    assert store.directory.stat().st_mode & 0o777 == 0o700
    assert "not protected" in data["provenance"]


def test_durable_atomic_writer_syncs_file_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced = []
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(os.fstat(fd).st_mode))
    atomic_write_text(tmp_path / "record.json", "{}\n", durable=True)
    assert len(synced) == 2
    assert (tmp_path / "record.json").read_bytes() == b"{}\n"


def test_post_replace_directory_sync_failure_cannot_leave_verified_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    store = records.AttemptStore.create(plan)
    (store.directory / "transcript.log").write_text("observed\n", encoding="ascii")
    original_sync = os.fsync

    def fail_directory_sync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync failed after replacement")
        original_sync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    result = RunResult(store.run_id, store.directory, Outcome.VERIFIED, None, ())
    with pytest.raises(ContractError) as failure:
        store.finish(result)
    assert failure.value.code == "finalization_failure"
    visible = json.loads(store.record_path.read_text(encoding="utf-8"))
    assert visible["complete"] is False
    assert visible["phase"] == "finalization_failed"
    assert visible["result"]["outcome"]["name"] == "HALTED"
    assert visible["result"]["stop_reason"] == "finalization_failure"
