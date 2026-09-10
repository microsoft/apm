"""Line-oriented presentation for contract plans and run observations."""

from __future__ import annotations

import os
import stat
import sys
from collections import deque
from pathlib import Path
from typing import BinaryIO

from apm_cli.contracts.models import (
    CheckObservation,
    ContractError,
    ContractLimits,
    FileEntry,
    LeafPlan,
    Outcome,
    RunEvent,
    RunResult,
)
from apm_cli.contracts.stream import safe_text
from apm_cli.utils import console

from .command_logger import CommandLogger


class _Transcript:
    """Bounded beginning/tail retention with exact omitted byte/line counts."""

    def __init__(self, limit: int) -> None:
        # Reserve room for the ASCII omission marker, even at the retention cap.
        self.budget = max(0, limit - 256)
        self.head: list[bytes] = []
        self.tail: deque[bytes] = deque()
        self.head_bytes = 0
        self.tail_bytes = 0
        self.omitted_bytes = 0
        self.omitted_lines = 0
        self.head_full = False

    def append(self, line: str) -> None:
        encoded = (line + "\n").encode("ascii")
        if not self.head_full and self.head_bytes + len(encoded) <= self.budget // 2:
            self.head.append(encoded)
            self.head_bytes += len(encoded)
            return
        self.head_full = True
        self.tail.append(encoded)
        self.tail_bytes += len(encoded)
        while self.tail and self.head_bytes + self.tail_bytes > self.budget:
            removed = self.tail.popleft()
            self.tail_bytes -= len(removed)
            self.omitted_bytes += len(removed)
            self.omitted_lines += 1

    def write(self, target: BinaryIO) -> None:
        for line in self.head:
            target.write(line)
        if self.omitted_lines:
            target.write(
                (
                    f"[i] Transcript truncated: {self.omitted_lines} lines / "
                    f"{self.omitted_bytes} bytes omitted between beginning and tail.\n"
                ).encode("ascii")
            )
        for line in self.tail:
            target.write(line)


class ContractLogger(CommandLogger):
    """The only human renderer for plans, supervised observations and results.

    close() freezes the transcript *before* the record owner hashes it. A later
    finished event renders the already-recorded result without touching logs.
    """

    def __init__(self, verbose: bool = False) -> None:
        super().__init__("contract", verbose=verbose)
        self._transcript = _Transcript(ContractLimits().transcript_bytes)
        self._file: BinaryIO | None = None
        self._run_id: str | None = None
        self._run_directory: Path | None = None
        self._closed = False
        self._finished = False
        self._human_enabled = True
        self._last_phase: str | None = None
        self._last_activity = 0.0

    @property
    def transcript_metadata(self) -> dict[str, int | str]:
        """Return owner-counted retention metadata, finalized by close().

        Each access returns a fresh snapshot; no transcript content is read.
        Omission counts refer to sanitized transcript bytes and logical lines.
        """
        return {
            "omitted_bytes": self._transcript.omitted_bytes,
            "omitted_lines": self._transcript.omitted_lines,
            "retention": "bounded-beginning-tail",
            "redaction": "best-effort",
        }

    def attach_run(self, run_id: str, run_directory: Path) -> None:
        """Exclusively create a private transcript in the admitted run directory."""
        if self._run_id is not None or self._closed:
            raise RuntimeError("A contract logger can only attach one run.")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(run_directory / "transcript.log", flags, 0o600)
        self._file = os.fdopen(descriptor, "wb")
        self._run_id = run_id
        self._run_directory = run_directory

    def close(self) -> None:
        """Flush once; errors propagate so recording cannot announce success."""
        if self._closed:
            return
        self._closed = True
        if self._file is not None:
            try:
                self._transcript.write(self._file)
                self._file.flush()
                os.fsync(self._file.fileno())
            finally:
                self._file.close()

    def _write(
        self,
        message: str,
        *,
        severity: str = "info",
        detail: bool = False,
        attribution: str | None = None,
    ) -> None:
        text = safe_text(message)
        prefix = f"{safe_text(attribution, limit=256)}: " if attribution else ""
        symbol, color = {
            "start": ("running", "blue"),
            "info": ("info", "blue"),
            "warning": ("warning", "yellow"),
            "error": ("error", "red"),
            "success": ("check", "green"),
            "detail": ("info", "dim"),
        }[severity]
        if not self._closed:
            self._transcript.append(f"{console.STATUS_SYMBOLS[symbol]} {prefix}{text}")
        if not self._human_enabled or (detail and not self.verbose):
            return
        try:
            # Keep paths and messages as intact logical lines in pipes and
            # terminals. The terminal may wrap visually; the renderer must not
            # inject newlines or continuation prefixes into copyable paths.
            console._rich_echo(
                prefix + text,
                color=color,
                symbol=symbol,
                propagate_broken_pipe=True,
                plain="NO_COLOR" in os.environ,
                natural_wrap=True,
            )
        except BrokenPipeError:
            # Do not recursively try to print an error into the closed pipe.
            # Observation, stream draining, recording and process cleanup remain.
            self._disable_human_output()

    def _disable_human_output(self) -> None:
        self._human_enabled = False
        # TextIO may retain a failed write and retry it during interpreter
        # shutdown. Silence only the already-broken human descriptor (stderr
        # in machine mode), without changing the healthy machine-output stream.
        stream = sys.stderr if console._console_stderr else sys.stdout
        try:
            descriptor = stream.fileno()
            mode = os.fstat(descriptor).st_mode
            if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
                return
        except (AttributeError, OSError, ValueError):
            return
        try:
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, descriptor)
            finally:
                os.close(null)
        except OSError:
            # Cleanup/recording still take precedence if the descriptor is
            # already closed or a process has exhausted its open-file limit.
            pass

    def on_event(self, event: RunEvent) -> None:
        """Consume the conductor's ordered stream; never derive an outcome."""
        handlers = {
            "selected": self._selected,
            "phase": self._phase,
            "activity": self._activity,
            "diagnostic": self._diagnostic,
            "metadata": self._metadata,
            "process_started": self._process_started,
            "stop_requested": self._stop_requested,
            "stop_observed": self._stop_observed,
            "check_started": self._check_started,
            "check_finished": self._check_finished,
            "finished": self._result,
            "heartbeat": self._heartbeat,
        }
        handler = handlers.get(event.kind)
        if handler is not None:
            handler(event)
            hidden_detail = event.kind in {"metadata", "process_started"} and not self.verbose
            if event.kind != "heartbeat" and not hidden_detail:
                self._last_activity = event.elapsed_seconds

    @staticmethod
    def _field(event: RunEvent, name: str, default: str = "unknown") -> str:
        value = event.data.get(name)
        return value if isinstance(value, str) else default

    def _selected(self, event: RunEvent) -> None:
        self._write(f"Contract: {self._field(event, 'contract')}", severity="start")
        self._write(
            f"Harness: {self._field(event, 'harness')}; "
            f"requested model: {self._field(event, 'model', 'native default')}"
        )
        self._write(f"Run: {event.run_id}")
        self._write(f"Record and logs: {self._field(event, 'run_directory')}")
        self._write(
            "Native host: advisory, not isolated. Consent does not override policy.",
            severity="warning",
        )

    def _phase(self, event: RunEvent) -> None:
        phase = self._field(event, "name")
        if phase == self._last_phase:
            return
        self._last_phase = phase
        self._write(phase.capitalize(), severity="start")

    def _attribution(self, event: RunEvent) -> str:
        source = "copilot" if event.source == "harness" else event.source
        label = self._field(event, "label", "")
        stream = self._field(event, "stream", "")
        parts = [source]
        if label:
            parts.append(label)
        if stream == "stderr":
            parts.append("stderr")
        return " ".join(parts) + " (untrusted)"

    def _activity(self, event: RunEvent) -> None:
        self._write(self._field(event, "text", ""), attribution=self._attribution(event))

    def _metadata(self, event: RunEvent) -> None:
        self._write(
            self._field(event, "text", ""),
            severity="detail",
            detail=True,
            attribution=self._attribution(event),
        )

    def _diagnostic(self, event: RunEvent) -> None:
        severity = self._field(event, "severity", "info")
        if severity not in {"info", "warning", "error"}:
            severity = "info"
        self._write(
            self._field(event, "message"),
            severity=severity,
            attribution=self._attribution(event) if event.source != "engine" else None,
        )
        action = self._field(event, "action", "")
        if action:
            self._write(action)

    def _process_started(self, event: RunEvent) -> None:
        pid = event.data.get("pid")
        pgid = event.data.get("pgid")
        self._write(
            f"Managed child started: pid={pid if isinstance(pid, int) else 'unknown'}, "
            f"pgid={pgid if isinstance(pgid, int) else 'unknown'}",
            severity="detail",
            detail=True,
        )

    def _stop_requested(self, event: RunEvent) -> None:
        self._write(
            f"Stop requested -- {self._field(event, 'reason')}; waiting for confirmation.",
            severity="warning",
        )

    def _stop_observed(self, event: RunEvent) -> None:
        if event.data.get("confirmed") is True:
            self._write("Original process-group stop confirmed; checking for provisional output.")
            self._write("Escaped descendants are unobserved.", severity="detail", detail=True)
        else:
            self._write(
                "Stop unconfirmed; a child may still be running. Inspect before retrying.",
                severity="error",
            )

    def _check_started(self, event: RunEvent) -> None:
        self._write(f"Check: {self._field(event, 'name')}", severity="start")

    def _check_finished(self, event: RunEvent) -> None:
        observation = event.data.get("observation")
        if not isinstance(observation, CheckObservation):
            raise TypeError("check_finished requires a CheckObservation.")
        status, severity = {
            0: ("passed", "success"),
            1: ("failed", "error"),
        }.get(observation.normalized, ("incomplete", "warning"))
        raw = observation.process.returncode
        raw_text = "no exit status" if raw is None else f"raw exit {raw}"
        self._write(f"{observation.name}: {status} ({raw_text})", severity=severity)
        if observation.normalized != 0:
            self._write(observation.reason)
            self._write("Inspect the check definition and retained transcript, then rerun.")

    def _heartbeat(self, event: RunEvent) -> None:
        elapsed = event.data.get("elapsed_seconds", event.elapsed_seconds)
        if not isinstance(elapsed, (int, float)) or elapsed - self._last_activity < 15:
            return
        self._write(f"Execution still running; {elapsed:.0f}s elapsed.")
        self._last_activity = elapsed

    def _result(self, event: RunEvent) -> None:
        if self._finished:
            return
        result = event.data.get("result")
        if not isinstance(result, RunResult):
            raise TypeError("finished requires a recorded RunResult.")
        self._finished = True
        passed = sum(check.normalized == 0 for check in result.checks)
        headline = f"{result.outcome.name} -- {passed}/{len(result.checks)} checks passed"
        if result.outcome == Outcome.VERIFIED:
            headline += "; native-advisory assessment"
        elif result.stop_reason:
            headline += f"; {result.stop_reason}; assessment incomplete"
        elif result.artifact is None:
            headline += "; declared output was not captured"
        self._write(
            headline,
            severity="success" if result.outcome == Outcome.VERIFIED else "error",
        )
        if result.artifact is not None:
            label = "Artifact" if result.outcome == Outcome.VERIFIED else "Provisional artifact"
            self._write(f"{label}: {result.artifact.path}")
        if result.outcome != Outcome.VERIFIED:
            self._write("Inspect the contract, retained record and transcript before retrying.")
        self._write(
            "Observed execution model: "
            + (", ".join(result.observed_models) if result.observed_models else "unknown")
        )
        self._write(f"Record and logs: {result.run_directory}")
        self._write("Private, best-effort redacted local logs; not protected provenance.")

    def render_plan(self, plan: LeafPlan, inventory: tuple[FileEntry, ...]) -> None:
        """Show the admitted surface without printing source bodies or prompts."""
        self._write("Plan only -- no execution", severity="start")
        self._write(f"Contract: {plan.contract.path}")
        self._write(f"Harness: {plan.harness}; requested model: {plan.model or 'native default'}")
        self._write(f"Native executable: {plan.executable}")
        self._write(
            f"Baseline: {len(inventory)} files, {sum(item.size for item in inventory)} bytes"
        )
        for name in plan.contract.needs:
            self._write(f"Input: {name}")
        self._write(f"Declared output: {plan.contract.produces}")
        self._write(f"Checks: {len(plan.contract.checks)}")
        for check in plan.contract.checks:
            self._write(f"Check: {check.name}")
            self._write(check.command, severity="detail", detail=True)
        self._write(f"Installed skills: {len(plan.imported_skills)}")
        for skill in plan.imported_skills:
            self._write(f"Imported skill: {skill.name}")
            identity = f"Source identity: {skill.lock_identity}; {skill.assurance}"
            if skill.assurance == "observed-local-source":
                identity += ", not a cryptographic pin"
            self._write(identity)
            self._write(
                f"Observed source SHA-256: {skill.source_digest}", severity="detail", detail=True
            )
            if skill.resolved_commit:
                self._write(
                    f"Resolved commit: {skill.resolved_commit}", severity="detail", detail=True
                )
        self._write(f"Policy: {plan.policy_status}")
        self._write(
            f"Watchdogs: attempt {plan.limits.attempt_seconds:g}s; "
            f"each check {plan.limits.check_seconds:g}s"
        )
        self._write(
            "Native execution is not isolated: host files, network and ambient credentials "
            "may be accessible. --allow-advisory is required in TTY and pipes; "
            "it does not override mandatory policy.",
            severity="warning",
        )

    def render_error(self, error: ContractError) -> None:
        """Render a pre-admission refusal without inventing a run or a success."""
        self._write(f"{error.outcome.name} -- {error.code}: {error}", severity="error")
        if error.location is not None:
            self._write(
                f"Source: {error.location.path}:{error.location.line}:{error.location.column}"
            )
