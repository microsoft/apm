"""Bounded native observations and one terminal/transcript safety path.

Native protocol objects are never forwarded. Text is withheld until a complete
line (or message) is available so redaction does not leak split credentials.
This is best-effort diagnostic redaction, not a safe-to-publish guarantee.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from apm_cli.utils.git_env import redact_git_diagnostic

from .events import EventEmitter
from .models import ContractLimits

_TEXT_BYTES = 16 * 1024
_DISPLAY_CHARS = 4096
_CORRELATED_MESSAGES = 64
_PUBLIC_PHASES = frozenset({"commentary", "final_answer"})


def safe_text(text: str, *, limit: int = _DISPLAY_CHARS) -> str:
    """Redact first, then visibly escape controls and Unicode without '?' loss.

    Source and artifact bytes are untouched. Backslash escapes retain readable,
    retrievable Unicode identity in the private diagnostic transcript.
    """
    redacted = redact_git_diagnostic(text)
    escaped = redacted.encode("unicode_escape", errors="backslashreplace").decode("ascii")
    # unicode_escape leaves a few ASCII controls (notably DEL) literal.
    escaped = "".join(
        character if " " <= character <= "~" else f"\\x{ord(character):02x}"
        for character in escaped
    )
    if len(escaped) > limit:
        suffix = " ... [text truncated]"
        return escaped[: max(0, limit - len(suffix))] + suffix
    return escaped


class _Lines:
    """Byte-bounded framing, draining oversized lines without exposing prefixes."""

    def __init__(
        self,
        limit: int,
        line: Callable[[bytes], None],
        overflow: Callable[[], None],
    ) -> None:
        self.limit = max(1, limit)
        self.line = line
        self.overflow = overflow
        self.pending = bytearray()
        self.discarding = False

    def feed(self, chunk: bytes) -> None:
        start = 0
        while start < len(chunk):
            newline = chunk.find(b"\n", start)
            end = len(chunk) if newline < 0 else newline
            if not self.discarding:
                if len(self.pending) + end - start > self.limit:
                    self.pending.clear()
                    self.discarding = True
                    self.overflow()
                else:
                    self.pending.extend(memoryview(chunk)[start:end])
            if newline < 0:
                return
            if not self.discarding:
                self.line(bytes(self.pending))
            self.pending.clear()
            self.discarding = False
            start = newline + 1

    def finish(self) -> None:
        if self.pending and not self.discarding:
            self.line(bytes(self.pending))
        self.pending.clear()
        self.discarding = False


@dataclass
class _Message:
    """Constant-space prefix correlation; no full-response accumulation."""

    lines: _Lines
    digest: object = field(default_factory=hashlib.sha256)
    length: int = 0
    complete: bool = False
    phase: str | None = None
    phase_known: bool = False
    suppressed: bool = False
    streamed: bool = False

    def delta(self, content: bytes, *, allow_stream: bool) -> None:
        # If the phase arrived late, the completion must provide the full text;
        # streaming only a suffix now would lose the earlier unknown prefix.
        can_stream = allow_stream and (self.length == 0 or self.streamed)
        self.digest.update(content)
        self.length += len(content)
        if can_stream:
            self.streamed = True
            self.lines.feed(content)


class ContractStreamDecoder:
    """Decode serialized supervisor byte callbacks into allowlisted observations."""

    def __init__(
        self,
        events: EventEmitter,
        *,
        limits: ContractLimits | None = None,
        source: Literal["harness", "checker"] = "harness",
        label: str | None = None,
        json_stdout: bool = True,
    ) -> None:
        self.events = events
        self.limits = limits or ContractLimits()
        self.source = source
        self.label = label
        self.json_stdout = json_stdout
        self.protocol_error: str | None = None
        self.completion_seen = False
        self._native_exit_code: int | None = None
        self._models: list[str] = []
        self._messages: dict[str, _Message] = {}
        self._unknown: set[str] = set()
        self._omission_notices: set[str] = set()
        self._closed = False
        self._stdout = (
            _Lines(
                min(self.limits.frame_bytes, 1024 * 1024),
                self._frame,
                lambda: self._protocol_failure("Native JSONL frame exceeded the byte limit."),
            )
            if json_stdout
            else self._text_lines("stdout")
        )
        self._stderr = self._text_lines("stderr")

    @property
    def observed_models(self) -> tuple[str, ...]:
        """Models explicitly reported by assistant, model-call or usage events."""
        return tuple(self._models)

    @property
    def native_exit_code(self) -> int | None:
        """Reported envelope status, not a child return code or APM outcome."""
        return self._native_exit_code

    def feed(self, stream: str, chunk: bytes) -> None:
        """Keep draining both streams even after a protocol or retention failure."""
        if self._closed:
            return
        if stream == "stdout":
            self._stdout.feed(chunk)
        elif stream == "stderr":
            self._stderr.feed(chunk)
        else:
            raise ValueError("Contract stream must be stdout or stderr.")

    def finish(self) -> None:
        """Flush trailing complete text/JSON exactly once."""
        if self._closed:
            return
        self._stdout.finish()
        self._stderr.finish()
        for message in self._messages.values():
            if message.phase in _PUBLIC_PHASES and not message.suppressed:
                message.lines.finish()
        self._closed = True

    def _emit(self, kind: str, **data: object) -> None:
        self.events.emit(kind, source=self.source, label=self.label, **data)

    def _activity(
        self, text: str, stream: str = "stdout", *, tool_status: str | None = None
    ) -> None:
        if text:
            # Both consumers use safe_text; keep raw bounded text in the
            # transient event only, avoiding a second escape of literal paths.
            self._emit(
                "activity",
                text=text,
                stream=stream,
                **({"tool_status": tool_status} if tool_status is not None else {}),
            )

    def _notice_once(self, key: str, message: str) -> None:
        if key not in self._omission_notices:
            self._omission_notices.add(key)
            self._emit(
                "diagnostic",
                severity="info",
                message=message,
                action="Inspect the native session if more detail is needed.",
            )

    def _text_lines(self, stream: str) -> _Lines:
        return _Lines(
            _TEXT_BYTES,
            lambda value: self._activity(value.decode("utf-8", errors="backslashreplace"), stream),
            lambda: self._notice_once(
                "long-text",
                "Oversized native text lines omitted; stream draining continues.",
            ),
        )

    def _protocol_failure(self, message: str) -> None:
        if self.protocol_error is None:
            self.protocol_error = message
            self._emit(
                "diagnostic",
                severity="error",
                message=message,
                action="Inspect the native CLI version and retained transcript before retrying.",
            )

    def _frame(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            event = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            self._protocol_failure("Native stdout contained malformed JSONL.")
            return
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            self._protocol_failure("Native JSONL event is missing its type.")
            return
        kind = event["type"]
        if len(kind) > 256:
            self._protocol_failure("Native JSONL event type exceeded the metadata limit.")
            return
        if kind == "result":
            self._native_result(event)
            return
        data = event.get("data", {})
        if not isinstance(data, dict):
            self._protocol_failure("Native JSONL event data must be an object.")
            return
        self._dispatch(kind, data)

    def _dispatch(self, kind: str, data: dict) -> None:
        handlers = {
            "assistant.message_start": self._message_start,
            "assistant.message_delta": self._delta,
            "assistant.message": self._message,
            "assistant.intent": self._intent,
            "tool.execution_start": self._tool_started,
            "tool.execution_complete": self._tool_finished,
            "session.error": self._session_error,
            "assistant.usage": self._observe_model,
            "model.call_start": self._observe_model,
            "session.usage": self._observe_model,
            "session.usage_info": self._observe_model,
        }
        if kind in {"assistant.idle", "session.idle", "session.shutdown"}:
            # The JSON CLI profile requires its final top-level result.
            # Assistant/session liveness cannot substitute for that envelope.
            self._emit("metadata", text=f"Native {kind.replace('.', ' ')}")
        elif kind in handlers:
            handlers[kind](data)
        elif kind not in self._unknown:
            if len(self._unknown) >= 32:
                self._notice_once("unknown", "Further unknown native event types omitted.")
                return
            self._unknown.add(kind)
            # No arbitrary data, keys, tool results, prompts or reasoning.
            self._emit("metadata", text=f"Native event not interpreted: {kind}")

    def _native_result(self, envelope: dict) -> None:
        """Accept the top-level result observed with Copilot 1.0.83-5.

        Only exitCode is retained. Session identity validates the envelope;
        usage/timestamp and all extra fields are deliberately not forwarded.
        In particular, no reasoning, encrypted content or raw JSON is logged.
        """
        code = envelope.get("exitCode")
        session_id = envelope.get("sessionId")
        if (
            not isinstance(code, int)
            or isinstance(code, bool)
            or not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 256
            or not isinstance(envelope.get("usage"), dict)
        ):
            self._protocol_failure("Native result envelope has invalid completion metadata.")
            return
        if self._native_exit_code is not None:
            if code != self._native_exit_code:
                self._protocol_failure("Native result envelopes report conflicting exit codes.")
            return
        self._native_exit_code = code
        # This flag records envelope observation, not successful execution.
        # The conductor must also check protocol_error and the OS observation.
        self.completion_seen = True
        self._emit(
            "metadata",
            text=f"Native completion reported exit code {code}; not an APM assessment.",
            native_exit_code=code,
        )
        if code != 0:
            self._protocol_failure(
                f"Native CLI reported exit code {code}; execution did not complete successfully."
            )

    def _get_message(self, data: dict) -> _Message | None:
        identifier = data.get("messageId")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
            self._protocol_failure("Native assistant message is missing a valid messageId.")
            return None
        if identifier not in self._messages:
            if len(self._messages) >= _CORRELATED_MESSAGES:
                self._notice_once(
                    "messages", "Message correlation limit reached; further response text omitted."
                )
                return None
            self._messages[identifier] = _Message(self._text_lines("stdout"))
        return self._messages[identifier]

    def _message_phase(self, message: _Message, data: dict) -> None:
        if "phase" not in data:
            return
        phase = data["phase"]
        if phase is not None and (not isinstance(phase, str) or len(phase) > 64):
            self._protocol_failure("Native assistant phase metadata is invalid.")
            message.suppressed = True
        elif message.phase_known and phase != message.phase:
            self._notice_once(
                "phase-change", "Native message phase changed; its response text is omitted."
            )
            message.suppressed = True
        message.phase_known = True
        message.phase = phase if isinstance(phase, str) else None
        if message.phase not in _PUBLIC_PHASES or message.suppressed:
            # Explicit null/tool, analysis and unknown phases are not public
            # assistant messages. Never release their content on finish().
            message.lines.pending.clear()

    def _message_start(self, data: dict) -> None:
        self._observe_model(data)
        message = self._get_message(data)
        if message is not None and not message.complete:
            self._message_phase(message, data)

    def _delta(self, data: dict) -> None:
        content = data.get("deltaContent")
        if not isinstance(content, str):
            self._protocol_failure("Native assistant delta is missing deltaContent text.")
            return
        message = self._get_message(data)
        if message is not None and not message.complete:
            self._message_phase(message, data)
            message.delta(
                content.encode("utf-8", errors="surrogatepass"),
                allow_stream=message.phase in _PUBLIC_PHASES and not message.suppressed,
            )

    def _message(self, data: dict) -> None:
        self._observe_model(data)
        content = data.get("content")
        if not isinstance(content, str):
            self._protocol_failure("Native assistant message is missing content text.")
            return
        message = self._get_message(data)
        if message is None or message.complete:
            return
        self._message_phase(message, data)
        if message.suppressed or (message.phase_known and message.phase not in _PUBLIC_PHASES):
            message.complete = True
            return
        # Legacy complete-message events without any phase remain supported.
        # Unknown-phase deltas are never released speculatively; a complete
        # message with an explicit private/null phase is suppressed above.
        encoded = content.encode("utf-8", errors="surrogatepass")
        prefix = encoded[: message.length]
        if not message.streamed:
            message.lines.feed(encoded)
        elif (
            len(prefix) == message.length
            and hashlib.sha256(prefix).digest() == message.digest.digest()
        ):
            message.lines.feed(encoded[message.length :])
        else:
            # Deltas are observations, not canonical final content. Keep the
            # correction explicit without repeating the entire final response.
            message.lines.pending.clear()
            self._notice_once(
                "correction",
                "Native final response differs from streamed text; "
                "bounded correction retained in the private transcript.",
            )
            # Verbose-only human detail, still retained through the same safety
            # path. Do not repeat the whole response in ordinary output.
            if len(encoded) <= _TEXT_BYTES:
                self._emit("metadata", text=f"Corrected final response: {content}")
            else:
                self._emit("metadata", text="Corrected final response exceeded the text limit.")
        message.lines.finish()
        message.complete = True

    def _intent(self, data: dict) -> None:
        intent = data.get("intent")
        if isinstance(intent, str):
            self._bounded_activity(intent)
        else:
            self._protocol_failure("Native assistant intent is missing intent text.")

    def _bounded_activity(self, text: str) -> None:
        if len(text.encode("utf-8", errors="surrogatepass")) > _TEXT_BYTES:
            self._notice_once("long-text", "Oversized native text omitted; draining continues.")
        else:
            self._activity(text)

    def _tool_started(self, data: dict) -> None:
        name = data.get("toolName")
        if isinstance(name, str) and len(name) <= 256:
            self._activity(f"Tool started: {name}", tool_status="started")
        else:
            self._activity("Tool started", tool_status="started")

    def _tool_finished(self, data: dict) -> None:
        status = {True: "completed", False: "failed"}.get(
            data.get("success") if isinstance(data.get("success"), bool) else None,
            "completion observed",
        )
        self._activity(f"Tool {status}", tool_status=status)

    def _session_error(self, data: dict) -> None:
        message = data.get("message")
        error_type = data.get("errorType")
        if not isinstance(message, str) or not isinstance(error_type, str):
            self._protocol_failure("Native session.error is missing its error type or message.")
            return
        # An explicit error cannot turn into success just because the process
        # later exits zero. Keep this diagnostic bounded even for hostile data.
        self.protocol_error = self.protocol_error or "Native session reported an error."
        action = "Inspect the native error and execution prerequisites, then retry."
        if error_type.lower() in {
            "authentication",
            "authentication_error",
            "not_authenticated",
            "unauthorized",
        }:
            action = "Run 'copilot login', then retry the contract."
        if len(message) > _TEXT_BYTES or len(error_type) > 256:
            message, error_type = "Error detail exceeded the text limit.", "native"
        self._emit(
            "diagnostic",
            severity="error",
            message=f"Native error ({error_type}): {message}",
            action=action,
        )

    def _observe_model(self, data: dict) -> None:
        model = data.get("model")
        usage = data.get("usage")
        if model is None and isinstance(usage, dict):
            model = usage.get("model")
        if model is None:
            return
        if not isinstance(model, str) or not model or len(model) > 256:
            self._protocol_failure("Native execution model metadata is invalid.")
        elif model not in self._models:
            if len(self._models) >= 16:
                self._protocol_failure("Native execution model count exceeded the metadata limit.")
                return
            self._models.append(model)
            self._emit("metadata", text=f"Observed execution model: {model}")
