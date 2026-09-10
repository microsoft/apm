"""Shared values for the bounded leaf-contract lifecycle."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Literal


class Outcome(IntEnum):
    """Command outcomes; assessment precedence belongs to records.py."""

    VERIFIED = 0
    REJECTED = 20
    UNPROVEN = 21
    HALTED = 22


@dataclass(frozen=True)
class SourceLocation:
    """A declaration location in the original contract."""

    path: Path
    line: int
    column: int = 1


class ContractError(ValueError):
    """An actionable contract refusal, distinct from legacy script errors."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_contract",
        location: SourceLocation | None = None,
        outcome: Outcome = Outcome.HALTED,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.location = location
        self.outcome = outcome


@dataclass(frozen=True)
class ContractLimits:
    """Admission and supervision limits, not native-host confinement."""

    source_bytes: int = 256 * 1024
    input_files: int = 16
    input_bytes: int = 16 * 1024 * 1024
    baseline_files: int = 10_000
    baseline_bytes: int = 128 * 1024 * 1024
    file_bytes: int = 8 * 1024 * 1024
    resource_files: int = 256
    resource_bytes: int = 8 * 1024 * 1024
    output_bytes: int = 4 * 1024 * 1024
    check_count: int = 8
    attempt_seconds: float = 1200
    check_seconds: float = 180
    cleanup_seconds: float = 6
    frame_bytes: int = 1024 * 1024
    transcript_bytes: int = 4 * 1024 * 1024


@dataclass(frozen=True)
class CheckSpec:
    """An uninterpreted command and its source location."""

    name: str
    command: str
    location: SourceLocation | None = None


@dataclass(frozen=True)
class LeafContract:
    """The supported agent-only source subset."""

    path: Path
    source_digest: str
    body: str
    needs: tuple[str, ...]
    produces: str
    checks: tuple[CheckSpec, ...]
    imports: tuple[str, ...] = ()
    locations: Mapping[str, SourceLocation] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportedSkill:
    """Selected installed context, with honest local-versus-pinned identity."""

    name: str
    source_path: Path
    content: str
    source_digest: str
    lock_identity: str
    resolved_commit: str | None = None
    verified_package_hash: str | None = None
    assurance: str = "observed-local-source"


@dataclass(frozen=True)
class ContractSource:
    """A selected package source, not an admission or isolation guarantee."""

    root: Path
    contract_relative_path: str
    package_ref: str | None = None
    resolved_commit: str | None = None
    package_hash: str | None = None
    assurance: str = "observed-local-source"
    prepared_hash: str | None = None
    original_root: Path | None = None
    original_manifest: bytes | None = None
    original_lock: bytes | None = None


@dataclass(frozen=True)
class LeafPlan:
    """Read-only plan; admission revalidates mutable sources."""

    contract: LeafContract
    project_root: Path
    executable: Path
    harness: str = "copilot"
    model: str | None = None
    executable_version: str | None = None
    imported_skills: tuple[ImportedSkill, ...] = ()
    limits: ContractLimits = field(default_factory=ContractLimits)
    policy_status: str = "no-policy"
    manifest_digest: str | None = None
    lock_digest: str | None = None
    source: ContractSource | None = None
    evidence_root: Path | None = None


@dataclass(frozen=True)
class FileEntry:
    """Exact captured regular-file identity."""

    relative_path: str
    sha256: str
    size: int
    mode: int


@dataclass(frozen=True)
class CapturedInput:
    """Exact source-to-baseline mapping; destination is owned by entry."""

    source_root: Path
    source_relative_path: str
    entry: FileEntry


@dataclass(frozen=True)
class BaselineSnapshot:
    """Independent captured baseline and producer working copy."""

    root: Path
    producer: Path
    files: tuple[FileEntry, ...]
    digest: str
    original_head: str | None
    synthetic_head: str
    resources_digest: str


@dataclass(frozen=True)
class Artifact:
    """Captured output, never a mutable producer-workspace alias."""

    relative_path: str
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class ProcessRequest:
    """One managed child, with no shell interpolation of native arguments."""

    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    env: Mapping[str, str] | None = None
    control_observations: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessObservation:
    """Observed termination and cleanup, not a full-tree containment claim."""

    returncode: int | None
    pid: int | None = None
    pgid: int | None = None
    elapsed_seconds: float = 0
    stop_reason: str | None = None
    error: str | None = None
    cleanup_confirmed: bool = True
    signals: tuple[str, ...] = ()
    residual_group: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class CheckObservation:
    """Raw process result alongside normalized assessment."""

    name: str
    command: str
    process: ProcessObservation
    normalized: int
    subject_digest: str
    resources_digest: str
    reason: str


@dataclass(frozen=True)
class RunResult:
    """Terminal local observation, reduced and persisted by one owner."""

    run_id: str
    run_directory: Path
    outcome: Outcome
    artifact: Artifact | None
    checks: tuple[CheckObservation, ...]
    stop_reason: str | None = None
    requested_model: str | None = None
    observed_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunEvent:
    """Small internal event; never the native harness's public protocol."""

    run_id: str
    sequence: int
    elapsed_seconds: float
    kind: str
    source: Literal["engine", "harness", "checker"]
    data: Mapping[str, object] = field(default_factory=dict)


EventSink = Callable[[RunEvent], None]
ByteSink = Callable[[str, bytes], None]
StartedSink = Callable[[int, int | None], None]
