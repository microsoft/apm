"""Deterministic native framing and safety tests; no model execution."""

import json
from dataclasses import replace

import pytest

from apm_cli.contracts.events import EventEmitter
from apm_cli.contracts.models import ContractLimits
from apm_cli.contracts.stream import ContractStreamDecoder, safe_text

pytestmark = pytest.mark.unit


def _frame(kind: str, **data: object) -> bytes:
    return (json.dumps({"type": kind, "data": data}, ensure_ascii=False) + "\n").encode("utf-8")


def _result_frame(**changes: object) -> bytes:
    """Harmless shape supplied from the preliminary native protocol probe."""
    envelope = {
        "type": "result",
        "exitCode": 0,
        "sessionId": "fixture-session",
        "timestamp": "2026-09-05T09:42:00Z",
        "usage": {
            "codeChanges": {},
            "premiumRequests": 1,
            "sessionDurationMs": 1000,
            "totalApiDurationMs": 800,
        },
    }
    envelope.update(changes)
    return (json.dumps(envelope) + "\n").encode()


def _decoder(**kwargs):
    observed = []
    decoder = ContractStreamDecoder(EventEmitter("test-run", observed.append), **kwargs)
    return decoder, observed


def _text(events) -> str:
    return "\n".join(safe_text(str(event.data.get("text", ""))) for event in events)


@pytest.mark.parametrize("phase", ["commentary", "final_answer"])
def test_bytewise_utf8_and_delta_final_correlation(phase: str) -> None:
    decoder, events = _decoder()
    wire = (
        _frame("assistant.message_start", messageId="a", phase=phase)
        + _frame("assistant.message_delta", messageId="a", deltaContent="Caf\u00e9 ")
        + _frame("assistant.message_delta", messageId="a", deltaContent="ready\n")
        + _frame("assistant.message", messageId="a", content="Caf\u00e9 ready\nDone")
        + _frame("assistant.message", messageId="a", content="Caf\u00e9 ready\nDone")
        + _frame("assistant.message_delta", messageId="a", deltaContent="duplicate")
        + _result_frame()
    )
    for byte in wire:
        decoder.feed("stdout", bytes([byte]))
    decoder.finish()
    decoder.finish()
    text = _text(events)
    assert text.count("Caf\\xe9 ready") == 1
    assert text.count("Done") == 1
    assert "duplicate" not in text
    assert decoder.completion_seen
    assert decoder.protocol_error is None
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))


def test_separate_streams_and_interleaved_message_ids() -> None:
    decoder, events = _decoder()
    decoder.feed("stdout", _frame("assistant.message_delta", messageId="a", deltaContent="one "))
    decoder.feed("stderr", b"Native connection ")
    decoder.feed("stdout", _frame("assistant.message", messageId="b", content="two"))
    decoder.feed("stderr", b"retry\n")
    decoder.feed("stdout", _frame("assistant.message", messageId="a", content="one done"))
    decoder.finish()
    assert [event.data["text"] for event in events] == [
        "two",
        "Native connection retry",
        "one done",
    ]
    assert events[1].data["stream"] == "stderr"
    assert all(event.source == "harness" for event in events)


@pytest.mark.parametrize("phase", ["commentary", "final_answer"])
def test_redaction_withheld_across_native_delta_and_byte_boundaries(phase: str) -> None:
    decoder, events = _decoder()
    secret = "ghp_" + "SPLITPRIVATE" * 3
    decoder.feed("stdout", _frame("assistant.message_start", messageId="a", phase=phase))
    decoder.feed("stdout", _frame("assistant.message_delta", messageId="a", deltaContent="token: "))
    decoder.feed(
        "stdout", _frame("assistant.message_delta", messageId="a", deltaContent=secret[:3])
    )
    assert _text(events) == ""
    for byte in _frame("assistant.message_delta", messageId="a", deltaContent=secret[3:] + "\n"):
        decoder.feed("stdout", bytes([byte]))
    decoder.feed("stderr", ("Authorization: Bearer " + secret[:5]).encode())
    decoder.feed("stderr", (secret[5:] + "\n").encode())
    decoder.finish()
    text = _text(events)
    assert secret not in text
    assert "SPLITPRIVATE" not in text
    assert "***" in text


def test_no_deltas_and_unterminated_final_frame() -> None:
    decoder, events = _decoder()
    decoder.feed("stdout", _frame("assistant.message", messageId="a", content="Done"))
    assert _text(events) == "Done"
    decoder.feed("stdout", _result_frame().rstrip(b"\n"))
    assert not decoder.completion_seen
    decoder.finish()
    assert decoder.completion_seen


@pytest.mark.parametrize(
    "wire",
    [
        b"not json\n",
        b'{"type":\xff}\n',
        b"[]\n",
        b"{}\n",
        b'{"type":"session.idle","data":[]}\n',
        _frame("assistant.message", messageId="a", content=[]),
        _frame("assistant.message", content="missing ID"),
        _frame("assistant.message_delta", messageId="a", deltaContent=7),
        _frame("assistant.intent", intent={}),
        _frame("session.error", errorType="unknown"),
        _frame("assistant.message", messageId="a", content="", model={}),
    ],
)
def test_malformed_protocol_is_sticky_but_streams_are_drained(wire: bytes) -> None:
    decoder, events = _decoder()
    decoder.feed("stdout", wire)
    decoder.feed("stdout", _result_frame())
    decoder.feed("stderr", b"Useful native failure detail\n")
    decoder.finish()
    assert decoder.protocol_error is not None
    assert decoder.completion_seen
    assert "Useful native failure detail" in _text(events)
    assert any(event.kind == "diagnostic" for event in events)


def test_oversized_frame_discards_prefix_and_recovers_at_newline() -> None:
    decoder, events = _decoder(limits=replace(ContractLimits(), frame_bytes=128))
    for _ in range(40):
        decoder.feed("stdout", b"TOPSECRET" * 7)
        assert len(decoder._stdout.pending) <= 128
    decoder.feed("stdout", b"\n" + _frame("assistant.intent", intent="Still draining"))
    decoder.feed("stdout", _result_frame(timestamp=None, usage={}))
    decoder.finish()
    assert decoder.protocol_error is not None
    assert decoder.completion_seen
    assert "TOPSECRET" not in _text(events)
    assert "Still draining" in _text(events)
    assert sum(event.kind == "diagnostic" for event in events) == 1


def test_hard_frame_limit_cannot_be_raised_past_one_mib() -> None:
    decoder, _ = _decoder(limits=replace(ContractLimits(), frame_bytes=8 * 1024 * 1024))
    decoder.feed("stdout", b"x" * (1024 * 1024 + 1))
    assert decoder.protocol_error is not None
    assert not decoder._stdout.pending


def test_checker_plain_text_utf8_stderr_and_no_newline() -> None:
    decoder, events = _decoder(source="checker", label="criterion", json_stdout=False)
    wire = "Criterion \u2713".encode()
    decoder.feed("stdout", wire[:-1])
    decoder.feed("stderr", b"stderr \xff\n")
    decoder.feed("stdout", wire[-1:])
    decoder.finish()
    assert [event.data["stream"] for event in events] == ["stderr", "stdout"]
    assert all(event.source == "checker" for event in events)
    assert all(event.data["label"] == "criterion" for event in events)
    assert "\\u2713" in _text(events)
    assert not decoder.completion_seen
    assert decoder.protocol_error is None


def test_long_plain_line_is_withheld_not_partially_redacted() -> None:
    decoder, events = _decoder(source="checker", json_stdout=False)
    decoder.feed("stdout", b"token=" + b"A" * 20_000)
    decoder.feed("stdout", b"private-tail\nNext line\n")
    decoder.finish()
    assert _text(events).endswith("Next line")
    assert "private-tail" not in _text(events)
    assert any("omitted" in str(event.data) for event in events)


def test_unknown_events_are_metadata_only_and_do_not_complete() -> None:
    decoder, events = _decoder()
    decoder.feed(
        "stdout",
        _frame(
            "future.event",
            exitCode=0,
            toolArguments="PRIVATE",
            reasoning="SECRET",
            model="invented",
        ),
    )
    decoder.feed("stdout", _frame("unrecognized", environment={"TOKEN": "private"}))
    decoder.feed("stdout", _frame("unrecognized", prompt="PRIVATE"))
    decoder.finish()
    assert all(event.kind == "metadata" for event in events)
    assert len(events) == 2
    assert "PRIVATE" not in str(events)
    assert "SECRET" not in str(events)
    assert decoder.observed_models == ()
    assert not decoder.completion_seen
    assert decoder.protocol_error is None


def test_tool_metadata_never_forwards_arguments_results_or_reasoning() -> None:
    decoder, events = _decoder()
    decoder.feed(
        "stdout",
        _frame(
            "tool.execution_start",
            toolName="read_file",
            arguments={"path": "PRIVATE"},
            reasoning="PRIVATE",
        ),
    )
    decoder.feed("stdout", _frame("tool.execution_complete", success=False, result="PRIVATE"))
    decoder.finish()
    assert _text(events) == "Tool started: read_file\nTool failed"
    assert "PRIVATE" not in str(events)
    assert not decoder.completion_seen


def test_only_explicit_execution_and_usage_models_are_observed() -> None:
    decoder, events = _decoder()
    decoder.feed("stdout", _frame("session.start", currentModel="not-observed"))
    decoder.feed("stdout", _frame("assistant.message", messageId="a", content="", model="actual-a"))
    decoder.feed("stdout", _frame("assistant.usage", model="actual-b"))
    decoder.feed(
        "stdout",
        _frame("assistant.message", messageId="b", content="", usage={"model": "actual-a"}),
    )
    decoder.finish()
    assert decoder.observed_models == ("actual-a", "actual-b")
    assert "not-observed" not in str(events)


@pytest.mark.parametrize(
    ("error_type", "auth_hint"),
    [("connection_error", False), ("launch_failed", False), ("authentication_error", True)],
)
def test_native_error_action_requires_explicit_auth_signal(
    error_type: str, auth_hint: bool
) -> None:
    decoder, events = _decoder()
    decoder.feed("stdout", _frame("session.error", errorType=error_type, message="Native failure"))
    decoder.feed("stdout", _frame("session.idle"))
    decoder.finish()
    assert decoder.protocol_error is not None
    diagnostic = next(event for event in events if event.kind == "diagnostic")
    assert ("copilot login" in diagnostic.data["action"]) is auth_hint


def test_metadata_and_correlation_state_are_bounded() -> None:
    decoder, events = _decoder()
    for number in range(100):
        decoder.feed("stdout", _frame(f"unknown.{number}", private="SECRET"))
        decoder.feed(
            "stdout", _frame("assistant.message_delta", messageId=str(number), deltaContent="x")
        )
    decoder.finish()
    assert len(decoder._unknown) == 32
    assert len(decoder._messages) == 64
    assert sum(event.kind == "diagnostic" for event in events) == 2
    assert "SECRET" not in str(events)


def test_safe_text_is_ascii_literal_redacted_and_identity_preserving() -> None:
    value = "path/\u00e9/\U0001f680 \x1b]52;c;clipboard\x07\r\x08\t\x7f\u202e"
    rendered = safe_text(value)
    assert all(" " <= character <= "~" for character in rendered)
    assert "?" not in rendered
    assert "\\xe9" in rendered
    assert "\\U0001f680" in rendered
    assert "\\x1b" in rendered
    assert "\\x7f" in rendered
    assert "\\u202e" in rendered
    assert safe_text("token=SUPERSECRET") == "token=***"
    assert len(safe_text("x" * 50_000)) == 4096


def test_changed_final_is_retained_as_detail_without_repeating_streamed_activity() -> None:
    decoder, events = _decoder()
    decoder.feed("stdout", _frame("assistant.message_start", messageId="a", phase="final_answer"))
    decoder.feed(
        "stdout", _frame("assistant.message_delta", messageId="a", deltaContent="Earlier\n")
    )
    decoder.feed("stdout", _frame("assistant.message", messageId="a", content="Corrected"))
    decoder.finish()
    assert [event.data["text"] for event in events if event.kind == "activity"] == ["Earlier"]
    assert [event.data["text"] for event in events if event.kind == "metadata"] == [
        "Corrected final response: Corrected"
    ]
    assert sum(event.kind == "diagnostic" for event in events) == 1


def test_whole_stream_and_arbitrary_chunking_have_identical_observations() -> None:
    wire = (
        _frame("assistant.intent", intent="Writing \u00e9")
        + _frame("assistant.message_delta", messageId="a", deltaContent="token=ghp_")
        + _frame("assistant.message_delta", messageId="a", deltaContent="PRIVATESECRET\n")
        + _frame("assistant.message", messageId="a", content="token=ghp_PRIVATESECRET\n")
        + _result_frame()
    )
    whole, expected = _decoder()
    whole.feed("stdout", wire)
    whole.finish()
    for size in (1, 2, 3, 7, 31, 128, 1024):
        decoder, actual = _decoder()
        for offset in range(0, len(wire), size):
            decoder.feed("stdout", wire[offset : offset + size])
        decoder.finish()
        assert [(event.kind, event.data) for event in actual] == [
            (event.kind, event.data) for event in expected
        ]
        assert decoder.completion_seen
        assert "PRIVATESECRET" not in _text(actual)


@pytest.mark.parametrize("exit_code", [0, 1, -1, 20, 130])
def test_observed_native_result_retains_status_and_rejects_failure(exit_code: int) -> None:
    decoder, events = _decoder()
    wire = _result_frame(exitCode=exit_code)
    for byte in wire:
        decoder.feed("stdout", bytes([byte]))
    decoder.feed("stdout", wire)
    decoder.finish()
    assert decoder.completion_seen
    assert (decoder.protocol_error is None) is (exit_code == 0)
    assert decoder.native_exit_code == exit_code
    assert len(events) == (1 if exit_code == 0 else 2)
    assert events[0].kind == "metadata"
    assert events[0].data["native_exit_code"] == exit_code
    assert "VERIFIED" not in _text(events)


@pytest.mark.parametrize(
    "changes",
    [
        {"exitCode": None},
        {"exitCode": "0"},
        {"exitCode": False},
        {"exitCode": 0.0},
        {"sessionId": None},
        {"sessionId": ""},
        {"sessionId": "x" * 257},
        {"usage": None},
        {"usage": []},
    ],
)
def test_malformed_result_cannot_establish_completion(changes: dict) -> None:
    decoder, events = _decoder()
    decoder.feed("stdout", _result_frame(**changes))
    decoder.finish()
    assert not decoder.completion_seen
    assert decoder.native_exit_code is None
    assert decoder.protocol_error is not None
    assert all(event.kind == "diagnostic" for event in events)


def test_result_requires_top_level_metadata_not_a_guessed_data_wrapper() -> None:
    decoder, _ = _decoder()
    decoder.feed("stdout", _frame("result", exitCode=0, sessionId="test", usage={}))
    decoder.finish()
    assert not decoder.completion_seen
    assert decoder.protocol_error is not None


@pytest.mark.parametrize("kind", ["assistant.idle", "session.idle", "session.shutdown"])
def test_idle_or_shutdown_cannot_substitute_for_final_result(kind: str) -> None:
    decoder, events = _decoder()
    decoder.feed("stdout", _frame(kind))
    assert not decoder.completion_seen
    assert _text(events) == f"Native {kind.replace('.', ' ')}"
    decoder.feed("stdout", _result_frame())
    decoder.finish()
    assert decoder.completion_seen


def test_probe_shape_observes_actual_model_without_forwarding_sensitive_fields() -> None:
    decoder, events = _decoder()
    decoder.feed(
        "stdout",
        _frame("model.call_start", model="gpt-6-astra", reasoning="PRIVATE_REASONING"),
    )
    decoder.feed(
        "stdout",
        _frame(
            "assistant.message_delta",
            messageId="a",
            deltaContent="Useful text",
            reasoningText="PRIVATE_REASONING",
            encryptedContent="PRIVATE_ENCRYPTED",
        ),
    )
    decoder.feed(
        "stdout",
        _frame(
            "assistant.message",
            messageId="a",
            content="Useful text",
            model="gpt-6-astra",
            reasoning="PRIVATE_REASONING",
            encrypted="PRIVATE_ENCRYPTED",
        ),
    )
    decoder.feed("stdout", _frame("assistant.idle"))
    decoder.feed(
        "stdout",
        _result_frame(
            reasoning="PRIVATE_REASONING",
            encryptedContent="PRIVATE_ENCRYPTED",
            usage={"reasoning": "PRIVATE_REASONING"},
        ),
    )
    decoder.finish()
    assert decoder.observed_models == ("gpt-6-astra",)
    assert decoder.native_exit_code == 0
    assert decoder.completion_seen
    assert _text(events).count("Useful text") == 1
    assert "PRIVATE_" not in str(events)
    assert all(event.kind in {"activity", "metadata"} for event in events)


def test_conflicting_results_and_earlier_protocol_error_remain_errors() -> None:
    decoder, _ = _decoder()
    decoder.feed("stdout", _result_frame(exitCode=0))
    decoder.feed("stdout", _result_frame(exitCode=1))
    decoder.finish()
    assert decoder.native_exit_code == 0
    assert decoder.protocol_error is not None

    decoder, _ = _decoder()
    decoder.feed("stdout", b"malformed\n" + _result_frame())
    decoder.finish()
    assert decoder.completion_seen
    assert decoder.protocol_error is not None


@pytest.mark.parametrize("phase", ["analysis", "reasoning", "encrypted", "future-phase", None])
def test_start_phase_suppresses_nonpublic_deltas_and_completion(phase: str | None) -> None:
    decoder, events = _decoder()
    decoder.feed(
        "stdout",
        _frame(
            "assistant.message_start",
            messageId="hidden",
            phase=phase,
            reasoning="PRIVATE_START",
        ),
    )
    decoder.feed(
        "stdout",
        _frame("assistant.message_delta", messageId="hidden", deltaContent="PRIVATE_DELTA\n"),
    )
    # The completion need not repeat the start event's phase.
    decoder.feed("stdout", _frame("assistant.message", messageId="hidden", content="PRIVATE_FINAL"))
    decoder.feed(
        "stdout",
        _frame("tool.execution_start", toolName="apply_patch", arguments="PRIVATE_ARGUMENTS"),
    )
    decoder.finish()
    assert _text(events) == "Tool started: apply_patch"
    assert "PRIVATE_" not in str(events)
    assert decoder.protocol_error is None


@pytest.mark.parametrize("phase", ["commentary", "final_answer"])
def test_public_phase_streams_before_completion_and_deduplicates_final(phase: str) -> None:
    decoder, events = _decoder()
    decoder.feed("stdout", _frame("assistant.message_start", messageId="public", phase=phase))
    decoder.feed(
        "stdout",
        _frame("assistant.message_delta", messageId="public", deltaContent="Writing handoff\n"),
    )
    assert _text(events) == "Writing handoff"
    assert not decoder.completion_seen
    decoder.feed(
        "stdout",
        _frame(
            "assistant.message",
            messageId="public",
            phase=phase,
            content="Writing handoff\n",
            reasoning="PRIVATE_REASONING",
            encryptedContent="PRIVATE_ENCRYPTED",
        ),
    )
    decoder.finish()
    assert _text(events) == "Writing handoff"
    assert "PRIVATE_" not in str(events)


@pytest.mark.parametrize("phase", ["commentary", "final_answer"])
def test_public_completed_message_without_deltas_is_shown_once(phase: str) -> None:
    decoder, events = _decoder()
    wire = _frame("assistant.message", messageId="public", phase=phase, content="Reading notes.")
    decoder.feed("stdout", wire)
    decoder.feed("stdout", wire)
    decoder.finish()
    assert _text(events) == "Reading notes."
    assert decoder.protocol_error is None


@pytest.mark.parametrize("phase", ["commentary", "final_answer"])
def test_public_trailing_line_is_flushed_when_process_ends(phase: str) -> None:
    decoder, events = _decoder()
    decoder.feed("stdout", _frame("assistant.message_start", messageId="public", phase=phase))
    decoder.feed(
        "stdout",
        _frame("assistant.message_delta", messageId="public", deltaContent="Reading notes."),
    )
    assert events == []
    decoder.finish()
    decoder.finish()
    assert _text(events) == "Reading notes."
    assert decoder.protocol_error is None


def test_unknown_phase_deltas_are_not_released_before_later_analysis_phase() -> None:
    decoder, events = _decoder()
    decoder.feed(
        "stdout",
        _frame("assistant.message_delta", messageId="unknown", deltaContent="PRIVATE_UNKNOWN\n"),
    )
    assert events == []
    decoder.feed(
        "stdout",
        _frame(
            "assistant.message",
            messageId="unknown",
            phase="analysis",
            content="PRIVATE_UNKNOWN\n",
        ),
    )
    decoder.finish()
    assert events == []


def test_unclassified_deltas_are_not_flushed_at_process_end() -> None:
    decoder, events = _decoder()
    decoder.feed(
        "stdout",
        _frame("assistant.message_delta", messageId="unknown", deltaContent="PRIVATE_PENDING"),
    )
    decoder.finish()
    assert events == []


@pytest.mark.parametrize("phase", ["commentary", "final_answer"])
def test_late_public_start_waits_for_full_message_without_losing_prefix(phase: str) -> None:
    decoder, events = _decoder()
    decoder.feed(
        "stdout", _frame("assistant.message_delta", messageId="late", deltaContent="Before ")
    )
    decoder.feed("stdout", _frame("assistant.message_start", messageId="late", phase=phase))
    decoder.feed(
        "stdout", _frame("assistant.message_delta", messageId="late", deltaContent="after\n")
    )
    assert events == []
    decoder.feed("stdout", _frame("assistant.message", messageId="late", content="Before after\n"))
    decoder.finish()
    assert _text(events) == "Before after"


@pytest.mark.parametrize("phase", ["commentary", "final_answer"])
def test_message_phase_is_correlated_per_id_and_not_promoted_after_conflict(phase: str) -> None:
    decoder, events = _decoder()
    for identifier, selected in (("private", "analysis"), ("public", phase)):
        decoder.feed(
            "stdout", _frame("assistant.message_start", messageId=identifier, phase=selected)
        )
    decoder.feed(
        "stdout", _frame("assistant.message_delta", messageId="private", deltaContent="PRIVATE\n")
    )
    decoder.feed(
        "stdout", _frame("assistant.message_delta", messageId="public", deltaContent="Public\n")
    )
    decoder.feed(
        "stdout",
        _frame(
            "assistant.message",
            messageId="private",
            phase=phase,
            content="PRIVATE_FINAL",
        ),
    )
    decoder.feed("stdout", _frame("assistant.message", messageId="public", content="Public\n"))
    decoder.finish()
    assert _text(events).count("Public") == 1
    assert "PRIVATE" not in str(events)
    assert any(event.kind == "diagnostic" for event in events)


def test_nonzero_result_is_sticky_even_after_idle_and_later_zero() -> None:
    decoder, events = _decoder()
    decoder.feed("stdout", _result_frame(exitCode=1))
    decoder.feed("stdout", _frame("assistant.idle"))
    decoder.feed("stdout", _result_frame(exitCode=0))
    decoder.finish()
    assert decoder.native_exit_code == 1
    assert decoder.protocol_error is not None
    assert "exit code 1" in decoder.protocol_error
    assert sum(event.kind == "diagnostic" for event in events) == 1
