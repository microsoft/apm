"""Line-oriented presentation for contract plans and run observations."""

from __future__ import annotations

import os
import stat
import sys
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from apm_cli.contracts import records
from apm_cli.contracts.events import HEARTBEAT_SECONDS
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
from apm_cli.utils.paths import portable_relpath

from .command_logger import CommandLogger

if TYPE_CHECKING:
    from rich.status import Status


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
        self._status: Status | None = None
        self._caller_root = Path.cwd()
        self._produces = "saved output"
        self._activity_label = "Working"
        self._checks_heading_shown = False

    def start_activity(self, message: str, *, announce: bool = True) -> None:
        """Animate quiet work using the install spinner, never in retained logs."""
        if self._closed:
            return
        self._activity_label = message
        if not self._human_enabled:
            if announce:
                self._write(message, severity="start")
            return
        from apm_cli.utils.install_tui import should_animate

        rich_console = console._get_console()
        animate = (
            not self._plain_output()
            and should_animate()
            and rich_console is not None
            and rich_console.is_terminal
        )
        if announce:
            self._write(message, severity="start", detail=animate)
        if not animate or not self._human_enabled:
            return
        from rich.text import Text

        label = Text(safe_text(message) + "...", style="default", no_wrap=True, overflow="ellipsis")
        try:
            if self._status is None:
                self._status = rich_console.status(
                    label, spinner="line", spinner_style="cyan", refresh_per_second=8
                )
                self._status.start()
            else:
                self._status.update(label)
        except BrokenPipeError:
            self._disable_human_output()

    def stop_activity(self) -> None:
        """Restore the terminal before reporting an error or a final result."""
        status, self._status = self._status, None
        if status is not None:
            try:
                status.stop()
            except BrokenPipeError:
                self._disable_human_output()

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
        self.stop_activity()
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
        accent: str = "",
        indent: int = 2,
    ) -> None:
        text = safe_text(message)
        source = safe_text(attribution, limit=256) if attribution else ""
        prefix = f"{source} > " if source else ""
        symbol, color = {
            "start": ("running", "cyan"),
            "info": ("", "default"),
            "heading": ("", "default"),
            "warning": ("warning", "yellow"),
            "error": ("error", "red"),
            "success": ("check", "green"),
            "detail": ("", "dim"),
        }[severity]
        # Native diagnostics retain their source, never an engine status symbol.
        marker = console.STATUS_SYMBOLS[symbol] + " " if symbol and not source else ""
        line = " " * indent + marker + prefix + text
        if not self._closed:
            retained_prefix = f"{source} (untrusted) > " if source else ""
            self._transcript.append(" " * indent + marker + retained_prefix + text)
        if not self._human_enabled or (detail and not self.verbose):
            return
        accent_length = (
            indent + len(marker) + len(safe_text(accent)) if accent else indent + len(marker)
        )
        if severity in {"detail", "heading"}:
            accent_length = len(line)
        try:
            # Keep paths and messages as intact logical lines in pipes and
            # terminals. The terminal may wrap visually; the renderer must not
            # inject newlines or continuation prefixes into copyable paths.
            console._rich_echo(
                line,
                color=color,
                bold=severity == "heading" or bool(accent),
                propagate_broken_pipe=True,
                plain=self._plain_output(),
                natural_wrap=True,
                accent_length=accent_length,
            )
        except BrokenPipeError:
            # Do not recursively try to print an error into the closed pipe.
            # Observation, stream draining, recording and process cleanup remain.
            self._disable_human_output()

    @staticmethod
    def _plain_output() -> bool:
        """Keep noninteractive output escape-free, even with forced progress."""
        if (
            "NO_COLOR" in os.environ
            or os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}
            or os.environ.get("TERM", "").strip().lower() in {"", "dumb"}
        ):
            return True
        rich_console = console._get_console()
        if rich_console is not None:
            return not rich_console.is_terminal
        stream = sys.stderr if console._console_stderr else sys.stdout
        return not stream.isatty()

    def _path(self, path: Path | str) -> str:
        """Keep saved paths copyable relative to the original caller, not cwd."""
        path = Path(path)
        if not path.is_absolute():
            path = self._caller_root / path
        return portable_relpath(path, self._caller_root)

    def _disable_human_output(self) -> None:
        self._human_enabled = False
        self.stop_activity()
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

    def _job_identity(self, source: Path | str, relative: str = "") -> str:
        """Prefer the selected contract's stable path over package-copy paths."""
        if relative:
            return relative
        identity = self._path(source)
        return Path(source).name if Path(identity).is_absolute() else identity

    def _selected(self, event: RunEvent) -> None:
        caller = self._field(event, "caller_root", "")
        if caller:
            self._caller_root = Path(caller)
        self._produces = self._field(event, "produces", "saved output")
        source = self._field(event, "contract")
        relative = self._field(event, "contract_relative_path", "")
        package = self._field(event, "package_ref", "")
        identity = self._job_identity(source, relative)
        model = self._field(event, "model", "default model")
        self._write(f"Job: {identity} -> {self._produces}", severity="heading", indent=0)
        self._write(f"Copilot / {model}")
        self._write("Running on your machine (not sandboxed).")
        self._write(f"Source: {self._path(source)}", severity="detail", detail=True)
        if package:
            self._write(f"Package: {package}", severity="detail", detail=True)
        self._write(f"Requested model: {model}", severity="detail", detail=True)
        self._write(f"Run: {event.run_id}", severity="detail", detail=True)
        self._write(
            f"Record directory: {self._field(event, 'run_directory')}",
            severity="detail",
            detail=True,
        )

    def _phase(self, event: RunEvent) -> None:
        phase = self._field(event, "name")
        if phase == self._last_phase:
            return
        self._last_phase = phase
        if phase == "checks":
            self._checks_heading()
        message = {
            "preflight": "Preparing files",
            "execution": "Running Copilot",
            "capture": "Saving output",
            "checks": f"Checking {self._produces}",
            "record": "Saving results",
        }.get(phase)
        if message:
            self.start_activity(message, announce=phase != "checks")

    def _attribution(self, event: RunEvent) -> str:
        source = {"harness": "Copilot", "checker": "Check"}.get(event.source, event.source)
        label = self._field(event, "label", "")
        stream = self._field(event, "stream", "")
        parts = [source]
        if label:
            parts.append(label)
        if stream == "stderr":
            parts.append("stderr")
        return " ".join(parts)

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
            self._write(
                action,
                attribution=self._attribution(event) if event.source != "engine" else None,
            )

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
        self.start_activity("Stopping processes", announce=False)
        self._write(
            "Stop requested; waiting for managed processes.",
            severity="warning",
        )
        self._write(f"Stop reason: {self._field(event, 'reason')}", severity="detail", detail=True)

    def _stop_observed(self, event: RunEvent) -> None:
        if event.data.get("confirmed") is True:
            self._write("Managed process group stopped; looking for output.")
            self._write("Escaped descendants are unobserved.", severity="detail", detail=True)
        else:
            self._write(
                "Stop unconfirmed; a child may still be running. Inspect before retrying.",
                severity="error",
            )

    def _check_started(self, event: RunEvent) -> None:
        self._checks_heading()
        self.start_activity(
            f"Checking {self._produces} ({self._field(event, 'name')})",
            announce=False,
        )

    def _checks_heading(self) -> None:
        if not self._checks_heading_shown:
            self._checks_heading_shown = True
            self._write("", indent=0)
            self._write(f"APM: checking {self._produces}", severity="heading", indent=0)

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
        summary = f"{observation.name}: {status}"
        self._write(summary, severity=severity, accent=summary)
        self._write(f"Check {observation.name}: {raw_text}", severity="detail", detail=True)
        if observation.normalized != 0:
            if observation.normalized == 2:
                self._write(
                    f"APM: check '{observation.name}': {self._incomplete_check_reason(observation)}"
                )
            self._write(
                f"APM: check '{observation.name}': {observation.reason}",
                severity="detail",
                detail=True,
            )

    @staticmethod
    def _incomplete_check_reason(observation: CheckObservation) -> str:
        """Explain observed process facts without inventing checker testimony."""
        process = observation.process
        if process.error:
            return process.error
        if process.stop_reason:
            return {
                "timeout": "The check exceeded its time limit.",
                "attempt_deadline": "The run exceeded its time limit.",
                "cancelled": "The check was interrupted.",
            }.get(process.stop_reason, f"The check stopped ({process.stop_reason}).")
        if not process.cleanup_confirmed:
            return "Process cleanup could not be confirmed."
        if process.returncode is None:
            return "No exit status was observed."
        if process.returncode < 0:
            return f"The check was terminated by signal {-process.returncode}."
        if process.returncode == 0:
            return observation.reason
        return f"The check exited with status {process.returncode}."

    def _heartbeat(self, event: RunEvent) -> None:
        elapsed = event.data.get("elapsed_seconds", event.elapsed_seconds)
        if (
            self._status is not None
            or not isinstance(elapsed, (int, float))
            or elapsed - self._last_activity < HEARTBEAT_SECONDS
        ):
            return
        self._write(f"{self._activity_label} -- still running; {elapsed:.0f}s elapsed.")
        self._last_activity = elapsed

    def _result(self, event: RunEvent) -> None:
        if self._finished:
            return
        result = event.data.get("result")
        if not isinstance(result, RunResult):
            raise TypeError("finished requires a recorded RunResult.")
        self._finished = True
        self.stop_activity()
        self._write("", indent=0)
        headline = f"APM: {result.outcome.name}"
        severity = {
            Outcome.VERIFIED: "success",
            Outcome.UNPROVEN: "warning",
            Outcome.REJECTED: "error",
            Outcome.HALTED: "error",
        }[result.outcome]
        self._write(
            f"{headline}  {event.elapsed_seconds:.1f}s",
            severity=severity,
            accent=headline,
            indent=0,
        )
        self._result_explanation(result)
        if result.artifact is not None:
            self._write(f"Output: {self._path(result.artifact.path)}")
        else:
            self._write("No output was saved.")
        self._write(f"Record: {self._path(result.run_directory / 'record.json')}")
        self._write(
            "Observed execution model: "
            + (", ".join(result.observed_models) if result.observed_models else "unknown"),
            severity="detail",
            detail=True,
        )
        if result.stop_reason:
            self._write(f"Stop reason: {result.stop_reason}", severity="detail", detail=True)
        self._write(
            f"Logs: {self._path(result.run_directory / 'transcript.log')}",
            severity="detail",
            detail=True,
        )
        self._write(
            "Logs may contain sensitive data. Review before sharing.",
            severity="detail",
            detail=True,
        )

    def _result_explanation(self, result: RunResult) -> None:
        """Explain the recorded outcome; never promote or downgrade it here."""
        if result.outcome == Outcome.VERIFIED:
            self._write("Contract checks passed.")
        elif result.outcome == Outcome.REJECTED:
            self._write("Contract checks found a problem.")
            self._write("Review the failed checks and saved output before retrying.")
        elif result.outcome == Outcome.UNPROVEN:
            if records.native_assurance_limited(result):
                self._write("Contract checks passed; this run was not sandboxed.")
            elif result.artifact is None:
                self._write("The declared output could not be checked.")
                self._write(
                    "Review the contract output path and Copilot diagnostics before retrying."
                )
            else:
                self._write("Checks could not establish a result.")
                self._write("Review incomplete checks and their prerequisites before retrying.")
        else:
            reason, action = {
                "cancelled": ("Run interrupted.", "Review any saved output before rerunning."),
                "producer_failed": (
                    "Copilot did not complete successfully.",
                    "Review Copilot diagnostics and logs before retrying.",
                ),
                "native_reported_failure": (
                    "Copilot reported a failure.",
                    "Review Copilot diagnostics and logs before retrying.",
                ),
                "native_protocol_error": (
                    "Copilot output could not be interpreted.",
                    "Review Copilot diagnostics and logs before retrying.",
                ),
                "native_completion_unobserved": (
                    "Copilot completion was not observed.",
                    "Review Copilot diagnostics and logs before retrying.",
                ),
                "attempt_deadline": (
                    "The run exceeded its time limit.",
                    "Review the contract workload before retrying.",
                ),
                "timeout": (
                    "The process exceeded its time limit.",
                    "Review the contract workload before retrying.",
                ),
                "producer_stop_unconfirmed": (
                    "Copilot may still be running.",
                    "Inspect the reported process before retrying.",
                ),
                "checker_stop_unconfirmed": (
                    "A check may still be running.",
                    "Inspect the reported process before retrying.",
                ),
            }.get(
                result.stop_reason,
                (
                    "The run stopped before it could finish.",
                    "Resolve the reported error before retrying.",
                ),
            )
            self._write(reason)
            self._write(action)

    def render_plan(self, plan: LeafPlan, inventory: tuple[FileEntry, ...]) -> None:
        """Show the admitted surface without printing source bodies or prompts."""
        relative = plan.source.contract_relative_path if plan.source else ""
        source = (
            (plan.source.original_root or plan.source.root) / relative
            if plan.source
            else plan.contract.path
        )
        identity = self._job_identity(source, relative)
        self._write(
            f"Preview: {identity} -> {plan.contract.produces}", severity="heading", indent=0
        )
        self._write(f"Copilot / {plan.model or 'default model'}")
        self._write("Nothing will execute or download.")
        for name in plan.contract.needs:
            self._write(f"Input: {name}")
        self._write("Checks: " + ", ".join(check.name for check in plan.contract.checks))
        for skill in plan.imported_skills:
            self._write(f"Imported skill: {skill.name}")
        self._write(
            f"Time limits: run {plan.limits.attempt_seconds:g}s; "
            f"each check {plan.limits.check_seconds:g}s"
        )
        self._write("Even with passing checks, a run returns UNPROVEN because it is not sandboxed.")
        self._write("To run, use apmx with --allow-host-access and without --plan.")
        self._write(f"Source: {self._path(source)}", severity="detail", detail=True)
        if plan.source and plan.source.package_ref:
            self._write(f"Package: {plan.source.package_ref}", severity="detail", detail=True)
        self._write(
            f"Requested model: {plan.model or 'default model'}", severity="detail", detail=True
        )
        self._write(f"Native executable: {plan.executable}", severity="detail", detail=True)
        self._write(
            f"Baseline: {len(inventory)} files, {sum(item.size for item in inventory)} bytes",
            severity="detail",
            detail=True,
        )
        for check in plan.contract.checks:
            self._write(f"Check {check.name}: {check.command}", severity="detail", detail=True)
        for skill in plan.imported_skills:
            identity = f"Source identity: {skill.lock_identity}; {skill.assurance}"
            if skill.assurance == "observed-local-source":
                identity += ", not a cryptographic pin"
            self._write(identity, severity="detail", detail=True)
            self._write(
                f"Observed source SHA-256: {skill.source_digest}", severity="detail", detail=True
            )
            if skill.resolved_commit:
                self._write(
                    f"Resolved commit: {skill.resolved_commit}", severity="detail", detail=True
                )
        self._write(f"Policy: {plan.policy_status}", severity="detail", detail=True)

    def render_error(self, error: ContractError) -> None:
        """Render a pre-admission refusal without inventing a run or a success."""
        self.stop_activity()
        headline = f"APM: {error.outcome.name}"
        self._write(
            headline,
            severity="warning" if error.outcome == Outcome.UNPROVEN else "error",
            accent=headline,
            indent=0,
        )
        self._write(str(error))
        self._write(f"Reason: {error.code}", severity="detail", detail=True)
        if error.location is not None:
            self._write(
                f"Source: {self._path(error.location.path)}:{error.location.line}:{error.location.column}"
            )
