"""Real local child/check processes with a deterministic producer adapter.

These fault-injection tests are not the live Copilot acceptance demonstration.
"""

import hashlib
import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from apm_cli.contracts import engine
from apm_cli.contracts.models import (
    Artifact,
    BaselineSnapshot,
    CheckSpec,
    ContractError,
    LeafContract,
    LeafPlan,
    Outcome,
    ProcessRequest,
)
from apm_cli.core.contract_logger import ContractLogger

pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(
        sys.platform == "win32", reason="The native contract execution profile is POSIX-only"
    ),
]


@pytest.mark.parametrize("failure_stage", ["close", "record_update"])
def test_finalization_failure_repairs_record_before_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    """Post-execution persistence failures cannot leave an apparently active attempt."""
    plan = _plan(tmp_path, ())
    _fake_adapter(monkeypatch, plan)
    logger = ContractLogger()
    if failure_stage == "close":
        close = logger.close

        def fail_close() -> None:
            close()
            raise OSError("transcript sync failed")

        monkeypatch.setattr(logger, "close", fail_close)
    else:
        update = engine.records.AttemptStore.update

        def fail_record(self, phase: str, **observations: object) -> None:
            if phase == "record":
                raise OSError("record update failed")
            update(self, phase, **observations)

        monkeypatch.setattr(engine.records.AttemptStore, "update", fail_record)
    with pytest.raises(ContractError) as failure:
        engine.run_contract(plan, logger=logger, allow_advisory=True)
    assert failure.value.code == "finalization_failure"
    record = next((tmp_path / ".apm" / "runs").glob("*/record.json"))
    data = json.loads(record.read_text(encoding="utf-8"))
    assert data["phase"] == "finalization_failed"
    assert data["complete"] is False
    assert data["result"]["outcome"]["name"] == "HALTED"


def _python_check(name: str, code: str) -> CheckSpec:
    return CheckSpec(name, f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}")


def _plan(tmp_path: Path, checks: tuple[CheckSpec, ...]) -> LeafPlan:
    source = tmp_path / "job.contract.md"
    source.write_text("---\nproduces: result.txt\n---\nWrite the result.\n", encoding="utf-8")
    (tmp_path / "input.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / "apm.yml").write_text(
        "name: contract-engine-fixture\nversion: 0.0.0\n", encoding="utf-8"
    )
    return LeafPlan(
        contract=LeafContract(
            path=source,
            source_digest=hashlib.sha256(source.read_bytes()).hexdigest(),
            body="Write the result.",
            needs=("input.txt",),
            produces="result.txt",
            checks=checks,
        ),
        project_root=tmp_path,
        executable=Path(sys.executable),
        model="gpt-6-astra",
    )


def _fake_adapter(
    monkeypatch: pytest.MonkeyPatch,
    plan: LeafPlan,
    *,
    produce: bool = True,
    exit_code: int = 0,
    reported_exit_code: int | None = None,
) -> None:
    event = json.dumps(
        {
            "type": "assistant.message",
            "data": {"messageId": "fake-message", "content": "Done", "model": "gpt-6-astra"},
        }
    )
    completed = json.dumps(
        {
            "type": "result",
            "exitCode": exit_code if reported_exit_code is None else reported_exit_code,
            "sessionId": "fake-native",
            "usage": {},
        }
    )
    code = (
        "from pathlib import Path\n"
        + ("Path('result.txt').write_bytes(Path('input.txt').read_bytes())\n" if produce else "")
        + f"print({event!r}, flush=True)\nprint({completed!r}, flush=True)\n"
        + f"raise SystemExit({exit_code})\n"
    )

    def build_request(
        selected: LeafPlan,
        snapshot: BaselineSnapshot,
        directory: Path,
        *,
        timeout_seconds: float,
    ) -> ProcessRequest:
        return ProcessRequest(
            argv=(sys.executable, "-c", code),
            cwd=snapshot.producer,
            timeout_seconds=timeout_seconds,
        )

    adapter = Mock()
    adapter.build_contract_request.side_effect = build_request
    monkeypatch.setattr(engine.frontend, "plan_contract", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        engine.RuntimeFactory, "get_runtime_by_name", lambda *args, **kwargs: adapter
    )


def test_real_child_capture_and_each_check_gets_fresh_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(
        tmp_path,
        (
            _python_check(
                "first",
                "from pathlib import Path; assert Path('result.txt').read_bytes() == b'hello\\n'; "
                "Path('check-local.txt').write_text('private')",
            ),
            _python_check(
                "second",
                "from pathlib import Path; assert not Path('check-local.txt').exists(); "
                "assert Path('result.txt').read_bytes() == b'hello\\n'",
            ),
        ),
    )
    _fake_adapter(monkeypatch, plan)
    result = engine.run_contract(plan, logger=ContractLogger(), allow_advisory=True)
    assert result.outcome == Outcome.UNPROVEN
    assert result.stop_reason is None
    assert result.artifact is not None
    assert result.artifact.path.read_bytes() == b"hello\n"
    assert result.artifact.sha256 == hashlib.sha256(b"hello\n").hexdigest()
    assert [check.normalized for check in result.checks] == [0, 0]
    assert result.observed_models == ("gpt-6-astra",)
    assert not (tmp_path / "result.txt").exists()
    assert not (tmp_path / "check-local.txt").exists()
    assert (result.run_directory / "record.json").is_file()
    record = json.loads((result.run_directory / "record.json").read_text(encoding="utf-8"))
    assert record["result"]["outcome"] == {"name": "UNPROVEN", "exit_code": 21}
    assert record["controls"]["isolation"] == "unavailable"
    assert record["result"]["stop_reason"] is None
    transcript = result.run_directory / "transcript.log"
    assert record["native_reported_exit_code"] == 0
    assert record["child_pid"] is None
    assert record["child_pgid"] is None
    assert record["producer"]["pid"] is not None
    assert record["checks"][0]["process"]["pid"] is not None
    assert record["transcript"]["sha256"] == hashlib.sha256(transcript.read_bytes()).hexdigest()
    assert record["transcript"]["size"] == transcript.stat().st_size
    assert record["transcript_retention"] == {
        "omitted_bytes": 0,
        "omitted_lines": 0,
        "retention": "bounded-beginning-tail",
        "redaction": "best-effort",
    }


def test_stale_output_cannot_satisfy_missing_new_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, (_python_check("condition", "raise SystemExit(0)"),))
    (tmp_path / "result.txt").write_bytes(b"old output")
    _fake_adapter(monkeypatch, plan, produce=False)
    result = engine.run_contract(plan, logger=ContractLogger(), allow_advisory=True)
    assert result.outcome == Outcome.UNPROVEN
    assert result.artifact is None
    assert result.checks == ()
    assert (tmp_path / "result.txt").read_bytes() == b"old output"


def test_failure_and_incomplete_keep_raw_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(
        tmp_path,
        (
            _python_check("failed", "raise SystemExit(1)"),
            _python_check("unknown", "raise SystemExit(127)"),
        ),
    )
    _fake_adapter(monkeypatch, plan)
    result = engine.run_contract(plan, logger=ContractLogger(), allow_advisory=True)
    assert result.outcome == Outcome.REJECTED
    assert [check.process.returncode for check in result.checks] == [1, 127]
    assert [check.normalized for check in result.checks] == [1, 2]
    assert result.artifact is not None


def test_failed_producer_keeps_captured_provisional_output_without_assessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, (_python_check("condition", "raise SystemExit(0)"),))
    _fake_adapter(monkeypatch, plan, exit_code=7)
    result = engine.run_contract(plan, logger=ContractLogger(), allow_advisory=True)
    assert result.outcome == Outcome.HALTED
    assert result.stop_reason == "producer_failed"
    assert result.artifact is not None
    assert result.artifact.path.read_bytes() == b"hello\n"
    assert result.checks == ()


def test_no_consent_allocates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(tmp_path, (_python_check("condition", "raise SystemExit(0)"),))
    create = Mock(side_effect=AssertionError("run must not be admitted"))
    monkeypatch.setattr(engine.records.AttemptStore, "create", create)
    with pytest.raises(ContractError) as failure:
        engine.run_contract(plan, logger=ContractLogger())
    assert failure.value.outcome == Outcome.UNPROVEN
    create.assert_not_called()
    assert not (tmp_path / ".apm").exists()


def test_native_reported_failure_cannot_pass_on_os_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, (_python_check("condition", "raise SystemExit(0)"),))
    _fake_adapter(monkeypatch, plan, reported_exit_code=7)
    result = engine.run_contract(plan, logger=ContractLogger(), allow_advisory=True)
    assert result.outcome == Outcome.HALTED
    assert result.checks == ()
    record = json.loads((result.run_directory / "record.json").read_text(encoding="utf-8"))
    assert record["producer"]["returncode"] == 0
    assert record["native_reported_exit_code"] == 7


@pytest.mark.parametrize("with_failure", [False, True])
def test_per_check_timeout_remains_incomplete_without_root_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_failure: bool
) -> None:
    checks = ((_python_check("failed", "raise SystemExit(1)"),) if with_failure else ()) + (
        _python_check("slow", "import time; time.sleep(30)"),
    )
    original = _plan(tmp_path, checks)
    plan = replace(original, limits=replace(original.limits, check_seconds=0.2))
    _fake_adapter(monkeypatch, plan)
    result = engine.run_contract(plan, logger=ContractLogger(), allow_advisory=True)
    assert result.outcome == (Outcome.REJECTED if with_failure else Outcome.UNPROVEN)
    assert result.stop_reason is None
    assert result.checks[-1].normalized == 2
    assert result.checks[-1].process.stop_reason == "timeout"


def test_lingering_check_child_is_an_operational_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(
        tmp_path,
        (
            _python_check(
                "lingers",
                "import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(30)'])",
            ),
        ),
    )
    _fake_adapter(monkeypatch, plan)
    result = engine.run_contract(plan, logger=ContractLogger(), allow_advisory=True)
    assert result.outcome == Outcome.HALTED
    assert result.stop_reason == "lingering_children"
    assert result.checks[0].normalized == 2


def test_changed_plan_refuses_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, (_python_check("condition", "raise SystemExit(0)"),))
    monkeypatch.setattr(
        engine.frontend,
        "plan_contract",
        lambda *args, **kwargs: replace(plan, model="different-model"),
    )
    with pytest.raises(ContractError) as failure:
        engine.run_contract(plan, logger=ContractLogger(), allow_advisory=True)
    assert failure.value.code == "plan_changed"
    assert not (tmp_path / ".apm").exists()


def test_check_copy_time_cannot_extend_the_attempt_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, (_python_check("condition", "raise SystemExit(0)"),))
    snapshot = BaselineSnapshot(tmp_path, tmp_path, (), "baseline", None, "head", "resources")
    artifact = Artifact("result.txt", tmp_path / "result.txt", "subject", 0)
    store = Mock(directory=tmp_path)
    events = Mock()
    monkeypatch.setattr(engine.workspace, "prepare_check_workspace", lambda *args: tmp_path)
    monkeypatch.setattr(engine.workspace, "verify_check_integrity", lambda *args: True)
    monkeypatch.setattr(engine.shutil, "which", lambda name: "/bin/sh")
    monkeypatch.setattr(
        engine,
        "_remaining",
        Mock(side_effect=[5, ContractError("Expired.", code="attempt_deadline")]),
    )
    launch = Mock(side_effect=AssertionError("no child may start after the deadline"))
    monkeypatch.setattr(engine.process, "supervise_process", launch)
    with pytest.raises(ContractError) as failure:
        engine._run_checks(plan, snapshot, artifact, store, events, 5, [])
    assert failure.value.code == "attempt_deadline"
    launch.assert_not_called()


def test_failed_record_finalization_never_announces_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, (_python_check("condition", "raise SystemExit(0)"),))
    _fake_adapter(monkeypatch, plan)
    monkeypatch.setattr(
        engine.records.AttemptStore,
        "finish",
        Mock(side_effect=OSError("record write failed")),
    )
    logger = Mock(transcript_metadata={})
    with pytest.raises(OSError, match="record write failed"):
        engine.run_contract(plan, logger=logger, allow_advisory=True)
    assert "finished" not in [call.args[0].kind for call in logger.on_event.call_args_list]
    logger.close.assert_called_once()


def test_dispatch_preparation_cannot_extend_the_attempt_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, (_python_check("condition", "raise SystemExit(0)"),))
    _fake_adapter(monkeypatch, plan)
    monkeypatch.setattr(
        engine,
        "_remaining",
        Mock(side_effect=[5, ContractError("Expired.", code="attempt_deadline")]),
    )
    result = engine.run_contract(plan, logger=ContractLogger(), allow_advisory=True)
    assert result.outcome == Outcome.HALTED
    assert result.stop_reason == "attempt_deadline"
    assert result.artifact is None
    assert result.checks == ()
    record = json.loads((result.run_directory / "record.json").read_text(encoding="utf-8"))
    assert "producer" not in record
