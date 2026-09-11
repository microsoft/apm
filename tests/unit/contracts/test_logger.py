"""Line output and private transcript tests; no native model invocation."""

import hashlib
import io
import json
import os
import sys
from dataclasses import replace
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
    ContractSource,
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
    ) is verbose
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
        for name in (
            "Preparing files",
            "Running Copilot",
            "Saving output",
            "APM: checking",
            "Saving results",
            "VERIFIED",
        )
    ]
    assert positions == sorted(positions)
    assert output.count("VERIFIED") == 1
    assert output.index("Job: hello.contract.md") < output.index("Preparing files")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert b"VERIFIED" not in path.read_bytes()
    assert "Observed execution model" not in output
    assert "Run: run-id" not in output
    assert "Run: run-id" in path.read_text()
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
    expected = "Managed process group stopped" if confirmed else "Stop unconfirmed"
    assert output.index("Stop requested") < output.index(expected) < output.index("HALTED")
    assert "Output:" in output
    assert "answer.txt" in output
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
    assert "Saving results" in output
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
    emitter.emit("phase", name="record")
    logger.close()
    emitter.emit("finished", result=_result(tmp_path, Outcome.HALTED, stop_reason="cancelled"))
    logger.close()
    assert len(calls) == 1
    assert "Still captured" in (tmp_path / "transcript.log").read_text()
    assert "Saving results" in (tmp_path / "transcript.log").read_text()


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


@pytest.mark.parametrize("verbose", [False, True])
def test_plan_is_nonexecuting_no_prompt_or_model_claim(
    capsys, tmp_path: Path, verbose: bool
) -> None:
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
    ContractLogger(verbose=verbose).render_plan(plan, (FileEntry("input.txt", "sha", 7, 0o644),))
    output = capsys.readouterr().out
    assert "Preview: hello.contract.md -> output.txt" in output
    assert "Nothing will execute or download." in output
    assert "Copilot / default model" in output
    assert "native default" not in output
    assert "Input: input.txt" in output
    assert "Checks: content" in output
    assert "Time limits: run" in output
    assert "UNPROVEN because it is not sandboxed." in output
    assert ("Baseline: 1 files, 7 bytes" in output) is verbose
    assert ("Native executable:" in output) is verbose
    assert ("Policy: no-policy" in output) is verbose
    assert ("Requested model:" in output) is verbose
    assert "DO NOT DUMP" not in output
    assert ("private check command" in output) is verbose
    assert "To run, use apmx with --allow-host-access and without --plan." in output
    assert "Imported skill:" not in output
    for stale in ("Installed skills: 0", "Watchdogs", "Harness:", "available login details", "[!]"):
        assert stale not in output
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
    assert "APM: HALTED" in output
    assert "Install the required executable and retry." in output
    assert "missing_executable" not in output
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
        "spinner_style": "cyan",
        "refresh_per_second": 8,
    }
    assert animated_console.status.call_args.args[0].plain == "Running Copilot..."
    events.emit("check_started", name="handoff")
    status.update.assert_called_once()
    assert status.update.call_args.args[0].plain == "Checking saved output (handoff)..."
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


@pytest.mark.parametrize("phase", ["commentary", "final_answer"])
def test_public_subprocess_output_flows_while_spinner_remains_active(
    tmp_path: Path, animated_console: Mock, phase: str
) -> None:
    logger = ContractLogger()
    logger.attach_run("run", tmp_path)
    logger.start_activity("Running Copilot")
    decoder = ContractStreamDecoder(EventEmitter("run", logger.on_event))
    for kind, data in [
        ("assistant.message_start", {"messageId": "public", "phase": phase}),
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
    assert "Copilot stderr > Native diagnostic" in output
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
    animated_console.status.return_value.start.side_effect = BrokenPipeError
    logger = ContractLogger()
    logger.start_activity("Preparing package")
    logger.start_activity("Running Copilot")
    logger.close()
    animated_console.status.assert_called_once()
    animated_console.status.return_value.stop.assert_called_once()
    assert not logger._human_enabled


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
        (
            "assistant.message_delta",
            {"messageId": "unlabeled", "deltaContent": "PRIVATE_UNKNOWN_PHASE\n"},
        ),
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
    assert "interrupted" in output
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
        assert "Managed process group stopped" in text
        assert "looking for output" in text
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
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("CI", "false")
    monkeypatch.setattr(console, "_get_console", lambda: Mock(is_terminal=True))
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
    assert f"Job: {source.name} -> saved output" in lines
    assert str(source) not in output
    assert f"  Output: {artifact_path}" in lines
    assert lines.count(f"  Record: {directory / 'record.json'}") == 1
    assert not any(line.startswith("[i]") for line in lines)


@pytest.mark.parametrize("verbose", [False, True])
def test_default_job_to_saved_output_story_and_verbose_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, verbose: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ".apm" / "runs" / "run"
    directory.mkdir(parents=True)
    logger = ContractLogger(verbose=verbose)
    logger.attach_run("run", directory)
    events = EventEmitter("run", logger.on_event)
    events.emit(
        "selected",
        contract=str(tmp_path / "jobs" / "handoff.contract.md"),
        caller_root=str(tmp_path),
        produces="handoff.json",
        model="requested-model",
        harness="copilot",
        run_directory=str(directory),
    )
    events.emit("phase", name="execution")
    events.emit("activity", source="harness", text="The requested output is ready.")
    events.emit("phase", name="checks")
    events.emit("check_started", name="handoff")
    events.emit(
        "activity",
        source="checker",
        label="handoff",
        text="Required fields present; 3 items checked.",
    )
    events.emit("check_finished", observation=_check(0, 0, "handoff"))
    events.emit("phase", name="record")
    logger.close()
    logger.on_event(
        RunEvent(
            "run",
            20,
            12.34,
            "finished",
            "engine",
            {
                "result": _result(
                    directory,
                    Outcome.UNPROVEN,
                    checks=(_check(0, 0, "handoff"),),
                    observed_models=("observed-model",),
                    artifact=Artifact(
                        "handoff.json", directory / "artifacts/handoff.json", "sha", 4
                    ),
                )
            },
        )
    )
    output = capsys.readouterr().out
    assert output.index("Job: jobs/handoff.contract.md -> handoff.json") < output.index("Copilot >")
    assert output.index("Copilot >") < output.index("APM: checking handoff.json")
    assert output.index("Check handoff > Required fields present") < output.index(
        "[+] handoff: passed"
    )
    assert output.index("[+] handoff: passed") < output.index("[!] APM: UNPROVEN  12.3s")
    assert "Contract checks passed; this run was not sandboxed." in output
    assert "  Output: .apm/runs/run/artifacts/handoff.json\n" in output
    assert "  Record: .apm/runs/run/record.json\n" in output
    for jargon in (
        "Preflight",
        "Execution",
        "Capture",
        "native-advisory",
        "(untrusted)",
        "Provisional",
        "[i]",
        "not protected provenance",
    ):
        assert jargon not in output
    assert output.count("requested-model") == 1 + int(verbose)
    assert "(requested)" not in output
    assert "[>] Checking handoff.json" not in output
    for detail in (
        "raw exit 0",
        "Run: run",
        "Source:",
        "observed-model",
        "Logs:",
        "Logs may contain sensitive data. Review before sharing.",
    ):
        assert (detail in output) is verbose
    transcript = (directory / "transcript.log").read_text()
    assert "Check handoff (untrusted) > Required fields present" in transcript
    assert "raw exit 0" in transcript
    assert "Run: run" in transcript
    assert "[!] APM: UNPROVEN" not in transcript


def test_saved_paths_are_relative_to_original_caller_after_cwd_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    caller = tmp_path / "caller"
    transient = tmp_path / "deleted-source-copy"
    caller.mkdir()
    transient.mkdir()
    monkeypatch.chdir(caller)
    logger = ContractLogger()
    directory = caller / ".apm/runs/stable"
    monkeypatch.chdir(transient)
    EventEmitter("run", logger.on_event).emit(
        "finished",
        result=_result(
            directory,
            Outcome.UNPROVEN,
            artifact=Artifact("output.txt", directory / "artifacts/output.txt", "digest", 1),
        ),
    )
    output = capsys.readouterr().out
    assert "Output: .apm/runs/stable/artifacts/output.txt" in output
    assert "Record: .apm/runs/stable/record.json" in output
    assert str(caller) not in output
    assert str(transient) not in output


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("local_package", [False, True])
def test_packaged_job_identity_uses_stable_contract_path(
    tmp_path: Path, capsys, verbose: bool, local_package: bool
) -> None:
    source = tmp_path / "private-source-copy" / "contract.contract.md"
    package_ref = str(tmp_path / "local-package") if local_package else "org/jobs/handoff#v1"
    events = EventEmitter("run", ContractLogger(verbose=verbose).on_event)
    events.emit(
        "selected",
        contract=str(source),
        contract_relative_path="contracts/handoff.contract.md",
        package_ref=package_ref,
        caller_root=str(tmp_path),
        produces="handoff.json",
        model="native-model",
    )
    output = capsys.readouterr().out
    assert "Job: contracts/handoff.contract.md -> handoff.json" in output
    assert ("private-source-copy" in output) is verbose
    assert (f"Package: {package_ref}" in output) is verbose
    assert output.count("native-model") == 1 + int(verbose)


@pytest.mark.parametrize(
    ("outcome", "reason", "message", "color"),
    [
        (Outcome.VERIFIED, None, "Contract checks passed.", "green"),
        (Outcome.REJECTED, None, "Contract checks found a problem.", "red"),
        (Outcome.UNPROVEN, None, "Checks could not establish a result.", "yellow"),
        (Outcome.HALTED, "cancelled", "Run interrupted.", "red"),
        (Outcome.HALTED, "producer_failed", "Copilot did not complete successfully.", "red"),
        (Outcome.HALTED, "checker_stop_unconfirmed", "A check may still be running.", "red"),
        (Outcome.HALTED, "attempt_deadline", "The run exceeded its time limit.", "red"),
    ],
)
def test_result_headline_color_and_reason_follow_owner(
    tmp_path: Path,
    animated_console: Mock,
    outcome: Outcome,
    reason: str | None,
    message: str,
    color: str,
) -> None:
    logger = ContractLogger()
    events = EventEmitter("run", logger.on_event)
    events.emit(
        "finished",
        result=_result(
            tmp_path,
            outcome,
            stop_reason=reason,
            artifact=Artifact("output.txt", tmp_path / "output.txt", "sha", 1),
        ),
    )
    calls = console._rich_echo.call_args_list
    headline = next(call for call in calls if f"APM: {outcome.name}" in call.args[0])
    assert headline.kwargs["color"] == color
    assert headline.args[0][: headline.kwargs["accent_length"]].endswith(outcome.name)
    assert any(message in call.args[0] for call in calls)
    assert any(call.args[0].startswith("  Output:") for call in calls)
    assert not any("provisional" in call.args[0].lower() for call in calls)


def test_animated_phases_are_transient_but_retained(tmp_path: Path, animated_console: Mock) -> None:
    logger = ContractLogger()
    logger.attach_run("run", tmp_path)
    events = EventEmitter("run", logger.on_event)
    for phase in ("preflight", "execution", "capture", "checks", "record"):
        events.emit("phase", name=phase)
    logger.close()
    written = "\n".join(call.args[0] for call in console._rich_echo.call_args_list)
    assert written.strip() == "APM: checking saved output"
    for label in (
        "Preparing files",
        "Running Copilot",
        "Saving output",
        "Saving results",
    ):
        assert label in (tmp_path / "transcript.log").read_text()
    status = animated_console.status.return_value
    status.start.assert_called_once()
    assert status.update.call_count == 4
    status.stop.assert_called_once()


@pytest.mark.parametrize("disabled", ["NO_COLOR", "CI", "TERM", "pipe"])
def test_forced_progress_cannot_override_plain_terminal_policy(
    animated_console: Mock, monkeypatch: pytest.MonkeyPatch, disabled: str
) -> None:
    monkeypatch.setenv("APM_PROGRESS", "always")
    if disabled == "pipe":
        animated_console.is_terminal = False
    else:
        monkeypatch.setenv(disabled, {"NO_COLOR": "", "CI": "true", "TERM": "dumb"}[disabled])
    logger = ContractLogger()
    logger.start_activity("Running Copilot")
    logger.close()
    animated_console.status.assert_not_called()
    assert console._rich_echo.call_args.kwargs["plain"]


def test_rich_accent_is_opt_in_and_never_styles_body_or_parses_markup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rich.console import Console

    monkeypatch.delenv("NO_COLOR")
    destination = io.StringIO()
    rich_console = Console(file=destination, force_terminal=True, color_system="standard")
    monkeypatch.setattr(console, "_get_console", lambda: rich_console)
    console._rich_echo(
        "[!] APM: UNPROVEN  [blue]literal[/blue]",
        color="yellow",
        bold=True,
        accent_length=len("[!] APM: UNPROVEN"),
        natural_wrap=True,
    )
    colored = destination.getvalue()
    assert "\x1b[1;33m[!] APM: UNPROVEN\x1b[0m" in colored
    assert "\x1b[39m  [blue]literal[/blue]\x1b[0m" in colored
    assert "\x1b[34m" not in colored
    destination.seek(0)
    destination.truncate()
    console._rich_echo("Legacy body", color="blue")
    assert "\x1b[34mLegacy body\x1b[0m" in destination.getvalue()


def test_checker_diagnostics_are_not_invented_from_passing_exit(capsys) -> None:
    events = EventEmitter("run", ContractLogger().on_event)
    events.emit("check_finished", observation=_check(0, 0, "nonempty"))
    text = capsys.readouterr().out
    assert text.strip() == "[+] nonempty: passed"
    assert "valid" not in text.lower()
    assert "schema" not in text.lower()


def test_changed_check_subject_explanation_is_not_hidden_with_raw_zero(capsys) -> None:
    events = EventEmitter("run", ContractLogger().on_event)
    events.emit(
        "check_finished",
        observation=CheckObservation(
            name="integrity",
            command="private command",
            process=ProcessObservation(returncode=0),
            normalized=2,
            subject_digest="sha",
            resources_digest="sha",
            reason="The supplied subject or check resources changed.",
        ),
    )
    output = capsys.readouterr().out
    assert "[!] integrity: incomplete" in output
    assert "APM: check 'integrity': The supplied subject or check resources changed." in output
    assert "Check integrity >" not in output
    assert "raw exit" not in output


@pytest.mark.parametrize("assurance_limited", [False, True])
def test_assurance_explanation_routes_through_record_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, assurance_limited: bool
) -> None:
    result = _result(
        tmp_path,
        Outcome.UNPROVEN,
        artifact=Artifact("output.txt", tmp_path / "output.txt", "sha", 1),
        checks=(_check(0, 0),),
    )
    owner = Mock(return_value=assurance_limited)
    monkeypatch.setattr("apm_cli.contracts.records.native_assurance_limited", owner)
    EventEmitter("run", ContractLogger().on_event).emit("finished", result=result)
    owner.assert_called_once_with(result)
    output = capsys.readouterr().out
    assert ("Contract checks passed; this run was not sandboxed." in output) is assurance_limited
    assert ("Checks could not establish a result." in output) is not assurance_limited
    assert "[!] APM: UNPROVEN" in output
    assert "Stop reason:" not in output


@pytest.mark.parametrize("verbose", [False, True])
def test_packaged_preview_uses_stable_identity_and_gates_source_metadata(
    tmp_path: Path, capsys, verbose: bool
) -> None:
    package = tmp_path / "local-package"
    disposable = tmp_path / "private-source-copy"
    relative = "contracts/handoff.contract.md"
    plan = LeafPlan(
        contract=LeafContract(
            disposable / relative,
            "source-digest",
            "PRIVATE_PROMPT",
            ("notes.txt",),
            "handoff.json",
            (CheckSpec("handoff", "PRIVATE_CHECK_COMMAND"),),
        ),
        project_root=tmp_path,
        executable=Path("/native/copilot"),
        model="fixture-model",
        source=ContractSource(
            root=disposable,
            contract_relative_path=relative,
            original_root=package,
            package_ref=str(package),
        ),
    )
    ContractLogger(verbose=verbose).render_plan(plan, ())
    output = capsys.readouterr().out
    assert "Preview: contracts/handoff.contract.md -> handoff.json" in output
    assert "Nothing will execute or download." in output
    assert "without --plan" in output
    assert ("Source: " + str(package / relative) in output) is verbose
    assert (f"Package: {package}" in output) is verbose
    assert "private-source-copy" not in output
    assert "PRIVATE_PROMPT" not in output
    assert ("PRIVATE_CHECK_COMMAND" in output) is verbose
    assert not (tmp_path / ".apm").exists()


def test_plain_checks_have_one_heading_without_duplicate_phase_narration(capsys) -> None:
    events = EventEmitter("run", ContractLogger().on_event)
    events.emit("selected", contract="job.contract.md", produces="handoff.json")
    events.emit("phase", name="checks")
    for name in ("format", "coverage"):
        events.emit("check_started", name=name)
        events.emit("activity", source="checker", label=name, text="Observed checker diagnostic.")
        events.emit("check_finished", observation=_check(0, 0, name))
    output = capsys.readouterr().out
    assert output.count("APM: checking handoff.json") == 1
    assert "Copilot / default model" in output
    assert "native default" not in output
    assert "[>] Checking" not in output
    assert "(saved output)" not in output
    assert "Check format > Observed checker diagnostic." in output
    assert "Check coverage > Observed checker diagnostic." in output
    assert "[+] format: passed" in output
    assert "[+] coverage: passed" in output


@pytest.mark.parametrize(
    ("process", "cause"),
    [
        (ProcessObservation(returncode=127), "The check exited with status 127."),
        (ProcessObservation(returncode=2), "The check exited with status 2."),
        (ProcessObservation(returncode=-9), "The check was terminated by signal 9."),
        (ProcessObservation(returncode=None), "No exit status was observed."),
        (
            ProcessObservation(returncode=None, error="Unable to create check workspace."),
            "Unable to create check workspace.",
        ),
        (
            ProcessObservation(returncode=-15, stop_reason="timeout"),
            "The check exceeded its time limit.",
        ),
        (
            ProcessObservation(returncode=0, cleanup_confirmed=False),
            "Process cleanup could not be confirmed.",
        ),
    ],
)
def test_incomplete_check_cause_is_visible_and_engine_owned(
    tmp_path: Path, capsys, process: ProcessObservation, cause: str
) -> None:
    logger = ContractLogger()
    logger.attach_run("run", tmp_path)
    events = EventEmitter("run", logger.on_event)
    events.emit(
        "check_finished",
        observation=replace(_check(process.returncode, 2), process=process),
    )
    logger.close()
    output = capsys.readouterr().out
    assert "[!] criterion: incomplete" in output
    assert f"APM: check 'criterion': {cause}" in output
    assert "Check criterion >" not in output
    assert "untrusted" not in (tmp_path / "transcript.log").read_text()
