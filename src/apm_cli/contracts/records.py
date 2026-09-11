"""One local authority for check normalization, outcomes and attempt records."""

import json
import os
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from uuid import uuid4

from ..utils.atomic_io import atomic_write_text
from ..utils.git_env import redact_git_diagnostic
from ..utils.path_security import has_symlink_component
from .models import (
    Artifact,
    CheckObservation,
    ContractError,
    ContractLimits,
    LeafPlan,
    Outcome,
    ProcessObservation,
    RunResult,
)


def normalize_check(process: ProcessObservation, *, integrity_ok: bool = True) -> int:
    """Retain raw exit separately; absence of prose does not weaken exit one."""
    if not integrity_ok or process.error or process.stop_reason or not process.cleanup_confirmed:
        return 2
    return process.returncode if process.returncode in (0, 1, 2) else 2


def reduce_outcome(
    artifact: Artifact | None,
    checks: tuple[CheckObservation, ...],
    stop_reason: str | None,
) -> Outcome:
    """Reduce native leaf results without claiming an enforced host boundary."""
    if stop_reason:
        return Outcome.HALTED
    if any(check.normalized == 1 for check in checks):
        return Outcome.REJECTED
    return Outcome.UNPROVEN


def native_assurance_limited(result: RunResult) -> bool:
    """Identify completed passing checks whose only limit is the native host."""
    return (
        result.outcome == Outcome.UNPROVEN
        and result.stop_reason is None
        and result.artifact is not None
        and bool(result.checks)
        and all(check.normalized == 0 for check in result.checks)
    )


def _json_value(value: object) -> object:
    if isinstance(value, IntEnum):
        return {"name": value.name, "exit_code": int(value)}
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


class AttemptStore:
    """Private, atomically updated local observations, not protected provenance."""

    def __init__(self, run_id: str, directory: Path, data: dict[str, object]) -> None:
        self.run_id = run_id
        self.directory = directory
        self.record_path = directory / "record.json"
        self._data = data

    @classmethod
    def create(cls, plan: LeafPlan) -> "AttemptStore":
        """Allocate unique run/attempt identity only after admission."""
        from .workspace import capture_provenance

        expected_parent = plan.project_root / ".apm" / "runs"
        parent = plan.evidence_root or expected_parent
        if parent != expected_parent:
            raise ContractError(
                "Run storage must remain caller-owned .apm/runs.", code="unsafe_run_directory"
            )
        if has_symlink_component(plan.project_root, parent):
            raise ContractError("Run storage contains a symlink.", code="unsafe_run_directory")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:12]
        directory = parent / run_id
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        retained, retained_identities = capture_provenance(plan, directory)
        store = cls(
            run_id,
            directory,
            {
                "schema": "apm-contract-run/0.1",
                "run_id": run_id,
                "attempt_id": run_id + "/1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "profile": "native-advisory",
                "provenance": "same-user local observations; not protected or signed",
                "phase": "admitted",
                "complete": False,
                "source": {
                    "path": str(plan.contract.path),
                    "sha256": plan.contract.source_digest,
                    "package": (
                        {
                            "root": plan.source.root,
                            "contract_relative_path": plan.source.contract_relative_path,
                            "package_ref": (
                                redact_git_diagnostic(plan.source.package_ref)
                                if plan.source.package_ref
                                else None
                            ),
                            "resolved_commit": plan.source.resolved_commit,
                            "package_hash": plan.source.package_hash,
                            "prepared_hash": plan.source.prepared_hash,
                            "original_root": plan.source.original_root,
                            "assurance": plan.source.assurance,
                        }
                        if plan.source
                        else None
                    ),
                    "retained": retained,
                    "retained_identities": retained_identities,
                },
                "caller_root": str(plan.project_root),
                "evidence_root": str(parent),
                "manifest_sha256": plan.manifest_digest,
                "lock_sha256": plan.lock_digest,
                "harness": plan.harness,
                "executable": str(plan.executable),
                "executable_version": plan.executable_version,
                "requested_model": plan.model,
                "observed_models": [],
                "imports": [
                    {
                        "name": skill.name,
                        "sha256": skill.source_digest,
                        "lock_identity": skill.lock_identity,
                        "assurance": skill.assurance,
                        "resolved_commit": skill.resolved_commit,
                    }
                    for skill in plan.imported_skills
                ],
                "limits": plan.limits,
                "controls": {
                    "isolation": "unavailable",
                    "spend_cap": "unavailable",
                    "process_cleanup": "original POSIX process group; escaped descendants unobserved",
                },
            },
        )
        store._write()
        return store

    def _write(self) -> None:
        atomic_write_text(
            self.record_path,
            json.dumps(_json_value(self._data), ensure_ascii=True, indent=2) + "\n",
            new_file_mode=0o600,
            durable=True,
        )

    def update(self, phase: str, **observations: object) -> None:
        """Commit observations without advertising a completed outcome."""
        self._data.update(observations)
        self._data["phase"] = phase
        self._write()

    def finish(self, result: RunResult) -> None:
        """Persist the final result before any terminal success announcement."""
        from .workspace import inspect_retained_log

        if (self.directory / "transcript.log").exists():
            self._data["transcript"] = inspect_retained_log(
                self.directory, ContractLimits().transcript_bytes
            )
        elif result.outcome == Outcome.VERIFIED or native_assurance_limited(result):
            raise ContractError("Final transcript is missing.", code="transcript_missing")
        self._data.update(
            complete=True,
            phase="finished",
            child_pid=None,
            child_pgid=None,
            active_check=None,
            finished_at=datetime.now(timezone.utc).isoformat(),
            result=result,
        )
        try:
            self._write()
        except OSError as exc:
            self.fail_finalization(result, exc)

    def fail_finalization(self, result: RunResult, error: Exception) -> None:
        """Retain the same incomplete state for transcript and record failures."""
        self._data.update(
            complete=False,
            phase="finalization_failed",
            result=replace(result, outcome=Outcome.HALTED, stop_reason="finalization_failure"),
        )
        try:
            self._write()
        except OSError as repair_error:
            raise ContractError(
                f"Run record finalization failed at {self.record_path}; failure-state "
                "persistence also failed. Treat this invocation as HALTED and do not "
                "rely on a visible success record.",
                code="finalization_failure",
            ) from repair_error
        raise ContractError(
            f"Run record finalization failed at {self.record_path}. The attempt is "
            "incomplete; inspect filesystem durability before retrying.",
            code="finalization_failure",
        ) from error
