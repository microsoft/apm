"""Line output and private transcript tests; no native model invocation."""

import hashlib
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import click
import pytest

from apm_cli.contracts.events import EventEmitter
from apm_cli.contracts.frontend import parse_contract
from apm_cli.contracts.models import (
    Artifact,
    CheckObservation,
    CheckSpec,
    ContractError,
    FileEntry,
    ImportedSkill,
    LeafContract,
    LeafPlan,
    Outcome,
    ProcessObservation,
    ProcessRequest,
    RunEvent,
    RunResult,
    SourceLocation,
)
from apm_cli.contracts.stream import ContractStreamDecoder
from apm_cli.core.contract_logger import ContractLogger, _Transcript
from apm_cli.core.output_mode import OutputMode, configure_output_mode
from apm_cli.utils import console

pytestmark = pytest.mark.component


@pytest.mark.parametrize("verbose", [False, True])
def test_reuse_plan_shows_resolved_skill_identity_before_consent(
    tmp_path: Path, capsys: pytest.CaptureFixture, verbose: bool
) -> None:
    examples = Path(__file__).resolve().parents[3] / "examples/contracts"
    source = examples / "handoff-style/SKILL.md"
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    plan = LeafPlan(
        contract=parse_contract(examples / "reuse-contract/handoff.contract.md"),
        project_root=tmp_path,
        executable=Path("/native/copilot"),
        imported_skills=(
            ImportedSkill("handoff-style", source, raw.decode("utf-8"), digest, "../handoff-style"),
        ),
    )
    ContractLogger(verbose=verbose).render_plan(plan, ())
    output = capsys.readouterr().out
    assert "Imported skill: handoff-style" in output
    assert (
        "Source identity: ../handoff-style; observed-local-source, not a cryptographic pin"
        in output
    )
    assert (digest in output) is verbose
    assert not (tmp_path / ".apm").exists()


@pytest.fixture(autouse=True)
def _plain_console(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "80")
    console._reset_console()
    yield
    console._reset_console()


def _check(raw: int | None, normalized: int, name: str = "criterion") -> CheckObservation:
    return CheckObservation(
        name=name,
        command="not printed",
        process=ProcessObservation(returncode=raw),
        normalized=normalized,
        subject_digest="subject",
        resources_digest="resources",
        reason=f"Check exited {raw}.",
    )


def _result(tmp_path: Path, outcome: Outcome, **kwargs) -> RunResult:
    return RunResult(
        run_id="run-id",
        run_directory=tmp_path,
        outcome=outcome,
        artifact=kwargs.pop("artifact", None),
        checks=kwargs.pop("checks", ()),
        **kwargs,
    )


def test_ordered_phases_final_once_and_frozen_transcript(tmp_path: Path, capsys) -> None:
    logger = ContractLogger()
    logger.attach_run("run-id", tmp_path)
    events = EventEmitter("run-id", logger.on_event)
    events.emit(
        "selected",
        contract="hello.contract.md",
        harness="copilot",
        model="requested",
        run_directory=str(tmp_path),
    )
    for phase in ("preflight", "execution", "capture", "checks", "record"):
        events.emit("phase", name=phase)
    logger.close()
    path = tmp_path / "transcript.log"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = _result(tmp_path, Outcome.VERIFIED, checks=(_check(0, 0),))
    events.emit("finished", result=result)
    events.emit("finished", result=result)
    logger.close()
    output = capsys.readouterr().out
    positions = [
        output.index(name)
        for name in ("Preflight", "Execution", "Capture", "Checks", "Record\n", "VERIFIED")
    ]
    assert positions == sorted(positions)
    assert output.count("VERIFIED") == 1
    assert output.index("Run: run-id") < output.index("Preflight")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert b"VERIFIED" not in path.read_bytes()
    assert "Observed execution model: unknown" in output
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("outcome", "stop"),
    [
        (Outcome.HALTED, "cancelled"),
        (Outcome.HALTED, "producer_failed"),
        (Outcome.REJECTED, None),
        (Outcome.UNPROVEN, None),
    ],
)
def test_final_outcome_is_authoritative_not_inferred(
    tmp_path: Path, capsys, outcome: Outcome, stop: str | None
) -> None:
    logger = ContractLogger()
    emitter = EventEmitter("run", logger.on_event)
    # A pass alone cannot override the record owner's outcome.
    emitter.emit(
        "finished", result=_result(tmp_path, outcome, stop_reason=stop, checks=(_check(0, 0),))
    )
    output = capsys.readouterr().out
    assert outcome.name in output
    assert "VERIFIED" not in output
    assert "[+]" not in output


@pytest.mark.parametrize(
    ("raw", "normalized", "text"),
    [
        (0, 0, "passed"),
        (1, 1, "failed"),
        (2, 2, "incomplete"),
        (127, 2, "incomplete"),
        (-9, 2, "incomplete"),
        (None, 2, "incomplete"),
    ],
)
def test_empty_stdout_check_status_comes_from_observation(
    capsys, raw: int | None, normalized: int, text: str
) -> None:
    emitter = EventEmitter("run", ContractLogger().on_event)
    emitter.emit("check_finished", observation=_check(raw, normalized))
    output = capsys.readouterr().out
    assert f"criterion: {text}" in output
    assert ("[+]" in output) is (normalized == 0)


@pytest.mark.parametrize("confirmed", [False, True])
def test_stop_request_precedes_observation_and_preserves_provisional_path(
    tmp_path: Path, capsys, confirmed: bool
) -> None:
    logger = ContractLogger()
    emitter = EventEmitter("run", logger.on_event)
    emitter.emit("stop_requested", reason="cancelled")
    emitter.emit("stop_observed", confirmed=confirmed, reason="cancelled")
    artifact = Artifact("answer.txt", tmp_path / "answer.txt", "identity", 3)
    emitter.emit(
        "finished",
        result=_result(tmp_path, Outcome.HALTED, stop_reason="cancelled", artifact=artifact),
    )
    output = capsys.readouterr().out
    expected = "Original process-group stop confirmed" if confirmed else "Stop unconfirmed"
    assert output.index("Stop requested") < output.index(expected) < output.index("HALTED")
    assert "Provisional artifact:" in output
    assert "answer.txt" in "".join(line[4:].strip() for line in output.splitlines())
    assert "cancelled successfully" not in output


@pytest.mark.parametrize("width", [30, 40, 80])
def test_narrow_no_color_controls_and_unicode_identity(
    tmp_path: Path, capsys, monkeypatch, width: int
) -> None:
    monkeypatch.setenv("COLUMNS", str(width))
    logger = ContractLogger()
    logger.attach_run("run", tmp_path)
    emitter = EventEmitter("run", logger.on_event)
    emitter.emit(
        "activity",
        source="harness",
        stream="stderr",
        text="Path/" + "long/" * 10 + "\u00e9/\x1b]52;c;evil\x07 \u202e\r[+] FAKE",
    )
    logger.close()
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert "Path/" + "long/" * 10 in output
    assert all(character == "\n" or " " <= character <= "~" for character in output)
    assert "?" not in output
    transcript = (tmp_path / "transcript.log").read_text()
    assert "\\xe9" in transcript
    assert "\\x1b" in transcript
    assert "\\u202e" in transcript
    assert "\\r[+] FAKE" in transcript
    assert "stderr" in transcript
    assert "untrusted" in transcript


def test_terminal_and_transcript_share_split_secret_redaction(tmp_path: Path, capsys) -> None:
    logger = ContractLogger()
    logger.attach_run("run", tmp_path)
    decoder = ContractStreamDecoder(EventEmitter("run", logger.on_event), json_stdout=False)
    secret = b"ghp_" + b"PRIVATE" * 5
    for byte in b"Authorization: Bearer " + secret + b"\n":
        decoder.feed("stderr", bytes([byte]))
    decoder.finish()
    logger.close()
    output = capsys.readouterr().out
    transcript = (tmp_path / "transcript.log").read_text()
    for rendered in (output, transcript):
        assert "PRIVATE" not in rendered
        assert "***" in rendered
        assert "stderr" in rendered


def test_retention_is_bounded_beginning_tail_and_reports_exact_omission(tmp_path: Path) -> None:
    transcript = _Transcript(2048)
    for number in range(200):
        transcript.append(f"line {number:03d} " + "x" * 80)
    path = tmp_path / "bounded.log"
    with path.open("wb") as target:
        transcript.write(target)
    data = path.read_bytes()
    assert len(data) <= 2048
    assert data.startswith(b"line 000")
    assert b"line 199" in data
    assert b"Transcript truncated" in data
    assert str(transcript.omitted_lines).encode() in data
    assert str(transcript.omitted_bytes).encode() in data
    assert len(transcript.head) + len(transcript.tail) + transcript.omitted_lines == 200


def test_transcript_saturation_does_not_hide_lifecycle_or_stderr(tmp_path: Path, capsys) -> None:
    logger = ContractLogger()
    logger._transcript = _Transcript(1024)
    logger.attach_run("run", tmp_path)
    emitter = EventEmitter("run", logger.on_event)
    for number in range(50):
        emitter.emit("activity", source="harness", text=f"Activity {number} " + "x" * 80)
    emitter.emit("activity", source="harness", stream="stderr", text="Useful last error")
    emitter.emit("phase", name="record")
    logger.close()
    emitter.emit(
        "finished", result=_result(tmp_path, Outcome.HALTED, stop_reason="producer_failed")
    )
    output = capsys.readouterr().out
    assert "Useful last error" in output
    assert "Record" in output
    assert "HALTED" in output
    assert (tmp_path / "transcript.log").stat().st_size <= 1024


def test_closed_pipe_disables_human_output_but_not_recording(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def broken(*args, **kwargs):
        calls.append(args)
        raise BrokenPipeError

    monkeypatch.setattr(console, "_rich_echo", broken)
    logger = ContractLogger()
    logger.attach_run("run", tmp_path)
    emitter = EventEmitter("run", logger.on_event)
    emitter.emit("phase", name="execution")
    emitter.emit("activity", source="harness", text="Still captured")
    emitter.emit("stop_requested", reason="cancelled")
    emitter.emit("stop_observed", confirmed=True, reason="cancelled")
    logger.close()
    emitter.emit("finished", result=_result(tmp_path, Outcome.HALTED, stop_reason="cancelled"))
    logger.close()
    assert len(calls) == 1
    assert "Still captured" in (tmp_path / "transcript.log").read_text()


def test_unrelated_renderer_errors_are_not_silently_swallowed(monkeypatch) -> None:
    def failed(*args, **kwargs):
        raise OSError("unexpected renderer failure")

    monkeypatch.setattr(console, "_rich_echo", failed)
    with pytest.raises(OSError, match="unexpected renderer failure"):
        ContractLogger().render_error(ContractError("Cannot proceed"))


def test_rich_broken_pipe_does_not_attempt_colorama_fallback(monkeypatch) -> None:
    class BrokenConsole:
        def print(self, *args, **kwargs):
            raise BrokenPipeError

    monkeypatch.setattr(console, "_get_console", lambda: BrokenConsole())
    monkeypatch.setattr(
        console.click, "echo", lambda *args, **kwargs: pytest.fail("fallback attempted")
    )
    with pytest.raises(BrokenPipeError):
        console._rich_echo("text")


def test_no_color_fallback_honors_stderr_routing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(console, "_get_console", lambda: None)
    configure_output_mode(OutputMode(machine_readable=True))
    ContractLogger().render_error(ContractError("Actionable failure"))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Actionable failure" in captured.err
    assert "\x1b" not in captured.err


def test_rich_honors_machine_output_routing(capsys) -> None:
    configure_output_mode(OutputMode(machine_readable=True))
    ContractLogger().render_error(ContractError("Inspect source"))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Inspect source" in captured.err


def test_plan_is_nonexecuting_no_prompt_or_model_claim(capsys, tmp_path: Path) -> None:
    plan = LeafPlan(
        contract=LeafContract(
            tmp_path / "hello.contract.md",
            "source",
            "DO NOT DUMP THIS PROMPT",
            ("input.txt",),
            "output.txt",
            (CheckSpec("content", "private check command"),),
        ),
        project_root=tmp_path,
        executable=Path("/native/copilot"),
        model=None,
    )
    ContractLogger().render_plan(plan, (FileEntry("input.txt", "sha", 7, 0o644),))
    output = capsys.readouterr().out
    assert "Plan only -- no execution" in output
    assert "Baseline: 1 files, 7 bytes" in output
    assert "requested model: native default" in output
    assert "DO NOT DUMP" not in output
    assert "private check command" not in output
    assert "--allow-host-access" in output
    assert "terminals and pipes" in output
    assert "available login details" in output
    assert "***" not in output
    assert not (tmp_path / "transcript.log").exists()


def test_pre_admission_error_has_source_location_not_fake_run(capsys, tmp_path: Path) -> None:
    ContractLogger().render_error(
        ContractError(
            "Install the required executable and retry.",
            code="missing_executable",
            location=SourceLocation(Path("job.contract.md"), 7, 3),
        )
    )
    output = capsys.readouterr().out
    assert "HALTED -- missing_executable" in output
    assert "job.contract.md:7:3" in output
    assert "copilot login" not in output
    assert "Run:" not in output


def test_heartbeat_is_liveness_not_fake_progress(capsys) -> None:
    emitter = EventEmitter("run", ContractLogger().on_event)
    emitter.emit("heartbeat", elapsed_seconds=4)
    emitter.emit("heartbeat", elapsed_seconds=5)
    emitter.emit("heartbeat", elapsed_seconds=6)
    emitter.emit("heartbeat", elapsed_seconds=10)
    output = capsys.readouterr().out
    assert output.count("still running") == 2
    assert "%" not in output


@pytest.mark.skipif(os.name != "posix", reason="Managed subprocesses require POSIX")
def test_quiet_subprocess_reports_liveness_within_six_seconds(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from apm_cli.contracts.process import supervise_process

    logger = ContractLogger()
    heartbeats = []

    def observe(event: RunEvent) -> None:
        if event.kind == "heartbeat":
            heartbeats.append(event.elapsed_seconds)
        logger.on_event(event)

    result = supervise_process(
        ProcessRequest(
            argv=(sys.executable, "-c", "import time; time.sleep(5.5)"),
            cwd=tmp_path,
            timeout_seconds=8,
        ),
        on_bytes=lambda stream, chunk: None,
        events=EventEmitter("run", observe),
    )
    logger.close()
    assert result.returncode == 0
    assert result.cleanup_confirmed
    assert len(heartbeats) == 1
    assert 5 <= heartbeats[0] < 6
    assert "still running; 5s elapsed" in capsys.readouterr().out


@pytest.fixture
def animated_console(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Use the install capability policy without a real refresh thread."""
    rich_console = Mock(is_terminal=True, is_interactive=True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("CI", "false")
    monkeypatch.setenv("APM_PROGRESS", "auto")
    monkeypatch.setattr(console, "_get_console", lambda: rich_console)
    monkeypatch.setattr("apm_cli.utils.install_tui._get_console", lambda: rich_console)
    monkeypatch.setattr(console, "_rich_echo", Mock())
    return rich_console


@pytest.mark.parametrize("finish", ["close", "error", "result"])
def test_spinner_starts_immediately_updates_and_stops(
    tmp_path: Path, animated_console: Mock, finish: str
) -> None:
    logger = ContractLogger()
    events = EventEmitter("run", logger.on_event)
    events.emit("phase", name="execution")
    status = animated_console.status.return_value
    status.start.assert_called_once()
    assert animated_console.status.call_args.kwargs == {
        "spinner": "line",
        "refresh_per_second": 8,
    }
    assert animated_console.status.call_args.args[0].plain == "Running Copilot..."
    events.emit("check_started", name="handoff")
    status.update.assert_called_once()
    assert status.update.call_args.args[0].plain == "Running check: handoff..."
    events.emit("heartbeat", elapsed_seconds=5)
    assert not any("still running" in call.args[0] for call in console._rich_echo.call_args_list)
    if finish == "close":
        logger.close()
    elif finish == "error":
        logger.render_error(ContractError("Cannot proceed"))
    else:
        events.emit("finished", result=_result(tmp_path, Outcome.HALTED))
    logger.close()
    status.stop.assert_called_once()
    assert logger._status is None


@pytest.mark.parametrize("disabled", ["NO_COLOR", "CI", "TERM", "APM_PROGRESS", "pipe"])
def test_noninteractive_progress_never_starts_a_spinner(
    tmp_path: Path, animated_console: Mock, monkeypatch: pytest.MonkeyPatch, disabled: str
) -> None:
    values = {"NO_COLOR": "1", "CI": "true", "TERM": "dumb", "APM_PROGRESS": "never"}
    if disabled == "pipe":
        animated_console.is_terminal = False
        monkeypatch.setenv("APM_PROGRESS", "always")
    else:
        monkeypatch.setenv(disabled, values[disabled])
    logger = ContractLogger()
    logger.attach_run("run", tmp_path)
    logger.start_activity("Preparing package")
    EventEmitter("run", logger.on_event).emit("heartbeat", elapsed_seconds=5)
    logger.close()
    animated_console.status.assert_not_called()
    transcript = (tmp_path / "transcript.log").read_text()
    assert "Preparing package" in transcript
    assert "still running; 5s elapsed" in transcript
    assert "\x1b" not in transcript


def test_public_subprocess_output_flows_while_spinner_remains_active(
    tmp_path: Path, animated_console: Mock
) -> None:
    logger = ContractLogger()
    logger.attach_run("run", tmp_path)
    logger.start_activity("Running Copilot")
    decoder = ContractStreamDecoder(EventEmitter("run", logger.on_event))
    for kind, data in [
        ("assistant.message_start", {"messageId": "public", "phase": "final_answer"}),
        ("assistant.message_delta", {"messageId": "public", "deltaContent": "Public line\n"}),
        ("assistant.intent", {"intent": "Reading input"}),
        ("tool.execution_start", {"toolName": "view", "arguments": "PRIVATE_ARGUMENTS"}),
        (
            "assistant.message",
            {"messageId": "hidden", "phase": "analysis", "content": "PRIVATE_ANALYSIS"},
        ),
    ]:
        decoder.feed("stdout", (json.dumps({"type": kind, "data": data}) + "\n").encode())
    decoder.feed("stderr", b"Native diagnostic\n")
    output = "\n".join(call.args[0] for call in console._rich_echo.call_args_list)
    assert "Public line" in output
    assert "Reading input" in output
    assert "Tool started: view" in output
    assert "copilot stderr (untrusted): Native diagnostic" in output
    assert "PRIVATE_" not in output
    animated_console.status.return_value.stop.assert_not_called()
    decoder.feed(
        "stdout",
        (
            json.dumps(
                {
                    "type": "assistant.message",
                    "data": {"messageId": "public", "content": "Public line\n"},
                }
            )
            + "\n"
        ).encode(),
    )
    decoder.finish()
    logger.close()
    transcript = (tmp_path / "transcript.log").read_text()
    assert transcript.count("Public line") == 1
    assert "Native diagnostic" in transcript
    assert "PRIVATE_" not in transcript
    assert "\x1b" not in transcript


def test_broken_pipe_stops_animation_without_losing_transcript(
    tmp_path: Path, animated_console: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = ContractLogger()
    logger.attach_run("run", tmp_path)
    logger.start_activity("Running Copilot")
    monkeypatch.setattr(console, "_rich_echo", Mock(side_effect=BrokenPipeError))
    events = EventEmitter("run", logger.on_event)
    events.emit("activity", source="harness", text="Captured despite closed output")
    logger.close()
    animated_console.status.return_value.stop.assert_called_once()
    assert "Captured despite closed output" in (tmp_path / "transcript.log").read_text()


def test_closed_output_cannot_start_animation(
    animated_console: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(console, "_rich_echo", Mock(side_effect=BrokenPipeError))
    logger = ContractLogger()
    logger.start_activity("Preparing package")
    logger.close()
    animated_console.status.assert_not_called()


def test_transcript_cannot_overwrite_or_follow_existing_path(tmp_path: Path) -> None:
    path = tmp_path / "transcript.log"
    path.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        ContractLogger().attach_run("run", tmp_path)
    assert path.read_bytes() == b"existing"


def test_close_failure_propagates_and_cannot_announce_success(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    logger = ContractLogger()
    logger.attach_run("run", tmp_path)

    def failed_sync(fd):
        raise OSError("disk failure")

    monkeypatch.setattr(os, "fsync", failed_sync)
    with pytest.raises(OSError, match="disk failure"):
        logger.close()
    logger.close()
    assert "VERIFIED" not in capsys.readouterr().out


@pytest.mark.parametrize("verbose", [False, True])
def test_native_result_zero_without_artifact_remains_unproven_and_private(
    tmp_path: Path, capsys, verbose: bool
) -> None:
    from apm_cli.contracts.records import reduce_outcome

    logger = ContractLogger(verbose=verbose)
    logger.attach_run("run", tmp_path)
    emitter = EventEmitter("run", logger.on_event)
    decoder = ContractStreamDecoder(emitter)
    wire = json.dumps(
        {
            "type": "result",
            "exitCode": 0,
            "sessionId": "harmless-fixture",
            "timestamp": "2026-09-05T09:42:00Z",
            "usage": {"reasoning": "PRIVATE_REASONING", "encrypted": "PRIVATE_ENCRYPTED"},
            "reasoning": "PRIVATE_REASONING",
            "encryptedContent": "PRIVATE_ENCRYPTED",
        }
    ).encode()
    decoder.feed("stdout", wire)
    decoder.finish()
    assert decoder.completion_seen
    assert decoder.native_exit_code == 0
    logger.close()
    result = _result(tmp_path, reduce_outcome(None, (), None))
    emitter.emit("finished", result=result)
    output = capsys.readouterr().out
    transcript = (tmp_path / "transcript.log").read_text()
    assert "UNPROVEN" in output
    assert "VERIFIED" not in output
    assert "[+]" not in output
    assert "Native completion reported exit code 0" in transcript
    assert "PRIVATE_" not in output + transcript
    assert "sessionId" not in output + transcript


@pytest.mark.parametrize("verbose", [False, True])
def test_analysis_phase_never_reaches_terminal_or_transcript(
    tmp_path: Path, capsys, verbose: bool
) -> None:
    logger = ContractLogger(verbose=verbose)
    logger.attach_run("run", tmp_path)
    decoder = ContractStreamDecoder(EventEmitter("run", logger.on_event))
    frames = [
        ("assistant.message_start", {"messageId": "hidden", "phase": "analysis"}),
        ("assistant.message_delta", {"messageId": "hidden", "deltaContent": "PRIVATE_ANALYSIS\n"}),
        ("assistant.message", {"messageId": "hidden", "content": "PRIVATE_ANALYSIS\n"}),
        ("tool.execution_start", {"toolName": "apply_patch", "arguments": "PRIVATE_TOOL_ARGS"}),
        ("assistant.message_start", {"messageId": "answer", "phase": "final_answer"}),
        ("assistant.message_delta", {"messageId": "answer", "deltaContent": "Public answer\n"}),
        (
            "assistant.message",
            {
                "messageId": "answer",
                "content": "Public answer\n",
                "phase": "final_answer",
                "model": "gpt-6-astra",
                "reasoning": "PRIVATE_REASONING",
                "encryptedContent": "PRIVATE_ENCRYPTED",
            },
        ),
    ]
    for kind, data in frames:
        wire = (json.dumps({"type": kind, "data": data}) + "\n").encode()
        for offset in range(0, len(wire), 7):
            decoder.feed("stdout", wire[offset : offset + 7])
    decoder.finish()
    logger.close()
    output = capsys.readouterr().out
    transcript = (tmp_path / "transcript.log").read_text()
    for text in (output, transcript):
        assert "PRIVATE_" not in text
        assert text.count("Public answer") == 1
        assert "Tool started: apply_patch" in text


def test_pre_engine_interrupt_reports_halted_without_claiming_child_cleanup(capsys) -> None:
    logger = ContractLogger()
    logger.render_error(
        ContractError(
            "Offline planning or inventory interrupted before native execution.",
            code="cancelled",
        )
    )
    logger.close()
    output = capsys.readouterr().out
    assert "HALTED" in output
    assert "cancelled" in output
    assert "Run:" not in output
    assert "stop confirmed" not in output
    assert "terminated" not in output
    assert "[+]" not in output


@pytest.mark.parametrize("verbose", [False, True])
def test_stop_confirmation_before_capture_does_not_claim_an_artifact(
    tmp_path: Path, capsys, verbose: bool
) -> None:
    logger = ContractLogger(verbose=verbose)
    logger.attach_run("run", tmp_path)
    emitter = EventEmitter("run", logger.on_event)
    emitter.emit("stop_observed", confirmed=True, reason="cancelled")
    logger.close()
    output = capsys.readouterr().out
    transcript = (tmp_path / "transcript.log").read_text()
    for text in (output, transcript):
        assert "Original process-group stop confirmed" in text
        assert "checking for provisional output" in text
        assert "retained" not in text
        assert "Artifact:" not in text
        assert "[+]" not in text
    assert "Escaped descendants are unobserved." in transcript
    assert ("Escaped descendants are unobserved." in output) is verbose


@pytest.mark.parametrize("stderr", [False, True])
def test_plain_contract_output_bypasses_nested_autoreset_without_mutating_legacy(
    monkeypatch, stderr: bool
) -> None:
    from colorama import AnsiToWin32, initialise

    target = io.StringIO()
    inner = AnsiToWin32(target, convert=False, strip=False, autoreset=True)
    outer = AnsiToWin32(inner.stream, convert=False, strip=False, autoreset=True)
    unregistered = []
    monkeypatch.setattr(console.atexit, "unregister", unregistered.append)
    monkeypatch.setattr(initialise, "atexit_done", True)
    monkeypatch.setattr(sys, "stderr" if stderr else "stdout", outer.stream)
    configure_output_mode(OutputMode(machine_readable=stderr))

    ContractLogger().render_error(ContractError("Plain contract refusal"))

    text = target.getvalue()
    assert "Plain contract refusal" in text
    assert "\x1b" not in text
    assert unregistered == [initialise.reset_all]
    assert not initialise.atexit_done
    assert inner.autoreset and outer.autoreset
    assert (sys.stderr if stderr else sys.stdout) is outer.stream
    outer.stream.write("Legacy write")
    assert "\x1b[0m" in target.getvalue()


def test_colored_contract_mode_preserves_legacy_autoreset_setup(monkeypatch) -> None:
    from colorama import initialise

    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr(initialise, "atexit_done", True)
    calls = []
    monkeypatch.setattr(
        console.atexit, "unregister", lambda fn: pytest.fail("colored mode changed exit hooks")
    )
    monkeypatch.setattr(console, "_rich_echo", lambda *args, **kwargs: calls.append(kwargs))
    ContractLogger().render_error(ContractError("Colored refusal"))
    assert calls
    assert all(not call["plain"] for call in calls)
    assert initialise.atexit_done


def test_transcript_metadata_is_read_only_and_returns_independent_snapshots() -> None:
    logger = ContractLogger()
    expected = {
        "omitted_bytes": 0,
        "omitted_lines": 0,
        "retention": "bounded-beginning-tail",
        "redaction": "best-effort",
    }
    assert logger.transcript_metadata == expected
    snapshot = logger.transcript_metadata
    snapshot["omitted_bytes"] = 999
    assert logger.transcript_metadata == expected
    with pytest.raises(AttributeError):
        logger.transcript_metadata = {}


def test_transcript_metadata_reports_counters_and_stays_frozen_after_close(
    tmp_path: Path,
) -> None:
    logger = ContractLogger()
    logger._human_enabled = False
    logger._transcript = _Transcript(512)
    logger.attach_run("run", tmp_path)
    lines = [f"entry {number:03d} " + "x" * 64 for number in range(20)]
    for line in lines:
        logger._transcript.append(line)
    expected_bytes = (
        sum(len(line.encode("ascii")) + 1 for line in lines)
        - logger._transcript.head_bytes
        - logger._transcript.tail_bytes
    )
    expected_lines = len(lines) - len(logger._transcript.head) - len(logger._transcript.tail)
    assert expected_bytes > 0 and expected_lines > 0
    logger.close()
    snapshot = logger.transcript_metadata
    assert snapshot["omitted_bytes"] == expected_bytes
    assert snapshot["omitted_lines"] == expected_lines
    path = tmp_path / "transcript.log"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    EventEmitter("run", logger.on_event).emit(
        "finished", result=_result(tmp_path, Outcome.UNPROVEN)
    )
    logger.close()
    assert logger.transcript_metadata == snapshot
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_live_telemetry_names_stay_quiet_without_hiding_stderr_or_native_errors(
    tmp_path: Path, capsys
) -> None:
    """Use event names observed in the retained live attempt, not its payloads."""
    telemetry = (
        "session.mcp_server_status_changed",
        "mcp.tools.list_changed",
        "session.mcp_servers_loaded",
        "session.skills_loaded",
        "session.tools_updated",
        "session.custom_agents_updated",
        "session.info",
        "session.canvas.registry_changed",
        "user.message",
        "assistant.turn_start",
        "assistant.tool_call_delta",
        "assistant.turn_end",
        "session.usage_checkpoint",
    )
    logger = ContractLogger()
    logger.attach_run("run", tmp_path)
    decoder = ContractStreamDecoder(EventEmitter("run", logger.on_event))
    for kind in telemetry:
        decoder.feed("stdout", (json.dumps({"type": kind, "data": {}}) + "\n").encode())
    # Synthetic fault observations: the retained live run had no stderr.
    decoder.feed("stderr", b"Actionable native stderr\n")
    decoder.feed(
        "stdout",
        (
            json.dumps(
                {
                    "type": "session.error",
                    "data": {"errorType": "connection_error", "message": "Connection dropped"},
                }
            )
            + "\n"
        ).encode(),
    )
    decoder.finish()
    logger.close()
    output = capsys.readouterr().out
    transcript = (tmp_path / "transcript.log").read_text()
    assert "Native event not interpreted" not in output
    assert transcript.count("Native event not interpreted") == len(telemetry)
    for text in (output, transcript):
        assert "Actionable native stderr" in text
        assert "Connection dropped" in text
        assert "Inspect the native error" in text
    assert "copilot login" not in output


@pytest.mark.parametrize("width", [30, 40, 80])
@pytest.mark.parametrize("tty", [False, True])
@pytest.mark.parametrize("no_color", [False, True])
@pytest.mark.parametrize("stderr", [False, True])
def test_full_source_artifact_and_log_paths_stay_copyable(
    tmp_path: Path, monkeypatch, capsys, width: int, tty: bool, no_color: bool, stderr: bool
) -> None:
    from rich.console import Console

    monkeypatch.setenv("COLUMNS", str(width))
    if not no_color:
        monkeypatch.delenv("NO_COLOR")
    configure_output_mode(OutputMode(machine_readable=stderr))
    monkeypatch.setattr(
        console, "_console_instance", Console(width=width, force_terminal=tty, stderr=stderr)
    )
    directory = tmp_path / "deep-source-component" / "session-state" / "contract-run-directory"
    source = directory / "source-with-a-long-name.contract.md"
    artifact_path = directory / "artifacts" / "handoff-with-a-long-name.json"
    logger = ContractLogger()
    emitter = EventEmitter("run", logger.on_event)
    emitter.emit(
        "selected",
        contract=str(source),
        harness="copilot",
        model="gpt-6-astra",
        run_directory=str(directory),
    )
    logger.close()
    emitter.emit(
        "finished",
        result=_result(
            directory,
            Outcome.HALTED,
            stop_reason="cancelled",
            artifact=Artifact("handoff.json", artifact_path, "identity", 1),
        ),
    )
    captured = capsys.readouterr()
    assert (captured.out if stderr else captured.err) == ""
    output = captured.err if stderr else captured.out
    if no_color:
        assert "\x1b" not in output
    lines = click.unstyle(output).splitlines()
    assert f"[>] Contract: {source}" in lines
    assert f"[i] Provisional artifact: {artifact_path}" in lines
    assert lines.count(f"[i] Record and logs: {directory}") == 2
    assert not any(line.startswith("[i]   ") or line.startswith("[>]   ") for line in lines)
