"""One per-command conductor for a captured, assessed agent leaf."""

import shutil
import time
from dataclasses import replace

from ..core.contract_logger import ContractLogger
from ..runtime.factory import RuntimeFactory
from . import frontend, process, records, workspace
from .events import EventEmitter
from .models import (
    Artifact,
    BaselineSnapshot,
    CheckObservation,
    ContractError,
    LeafPlan,
    Outcome,
    ProcessObservation,
    ProcessRequest,
    RunResult,
)
from .stream import ContractStreamDecoder


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ContractError("The attempt watchdog expired.", code="attempt_deadline")
    return remaining


def _producer_failure(observation: ProcessObservation) -> str | None:
    if observation.stop_reason:
        return observation.stop_reason
    if not observation.cleanup_confirmed:
        return "producer_stop_unconfirmed"
    if observation.error or observation.returncode != 0:
        return "producer_failed"
    return None


def _run_checks(
    plan: LeafPlan,
    snapshot: BaselineSnapshot,
    artifact: Artifact,
    store: records.AttemptStore,
    events: EventEmitter,
    deadline: float,
    observations: list[CheckObservation],
) -> str | None:
    """Run every eligible criterion against an independent captured subject."""
    shell = shutil.which("sh")
    for check in plan.contract.checks:
        _remaining(deadline)
        events.emit("check_started", name=check.name)
        check_root = workspace.prepare_check_workspace(
            snapshot, artifact, store.directory, check.name
        )
        integrity_ok = workspace.verify_check_integrity(snapshot, artifact, check_root)
        decoder = ContractStreamDecoder(
            events,
            limits=plan.limits,
            source="checker",
            label=check.name,
            json_stdout=False,
        )
        if not integrity_ok:
            observation = ProcessObservation(
                returncode=None, error="The supplied subject or check resources changed."
            )
        elif shell is None:
            observation = ProcessObservation(
                returncode=None, error="Required check shell 'sh' was not found on PATH."
            )
        else:
            store.update("checks", active_check=check.name)
            request = ProcessRequest(
                argv=(shell, "-c", check.command),
                cwd=check_root,
                timeout_seconds=min(plan.limits.check_seconds, _remaining(deadline)),
            )
            observation = process.supervise_process(
                request,
                on_bytes=decoder.feed,
                on_started=lambda pid, pgid, name=check.name: store.update(
                    "checks", active_check=name, child_pid=pid, child_pgid=pgid
                ),
                events=events,
                limits=plan.limits,
            )
        decoder.finish()
        integrity_ok = integrity_ok and workspace.verify_check_integrity(
            snapshot, artifact, check_root
        )
        normalized = records.normalize_check(observation, integrity_ok=integrity_ok)
        reason = (
            observation.error
            or observation.stop_reason
            or (
                f"Check '{check.name}' exited {observation.returncode}."
                if integrity_ok
                else "The supplied subject or check resources changed."
            )
        )
        checked = CheckObservation(
            name=check.name,
            command=check.command,
            process=observation,
            normalized=normalized,
            subject_digest=artifact.sha256,
            resources_digest=snapshot.resources_digest,
            reason=reason,
        )
        observations.append(checked)
        store.update("checks", checks=tuple(observations), active_check=None)
        events.emit("check_finished", observation=checked)
        if observation.stop_reason and observation.stop_reason != "timeout":
            return observation.stop_reason
        if not observation.cleanup_confirmed:
            return "checker_stop_unconfirmed"
        if time.monotonic() >= deadline:
            return "attempt_deadline"
    return None


def run_contract(
    plan: LeafPlan,
    *,
    logger: ContractLogger,
    allow_advisory: bool = False,
) -> RunResult:
    """Admit, execute, capture, assess and atomically record one fresh run."""
    if not allow_advisory:
        raise ContractError(
            "Native execution is not isolated. Run again with --allow-advisory "
            "only if host filesystem, network and ambient credential access are acceptable. "
            "This consent does not override policy.",
            code="advisory_consent_required",
            outcome=Outcome.UNPROVEN,
        )
    current_plan = frontend.plan_contract(
        plan.contract.path,
        plan.project_root,
        harness=plan.harness,
        model=plan.model,
        limits=plan.limits,
        source=plan.source,
    )
    if current_plan != plan:
        raise ContractError(
            "Contract source, installed context or native prerequisites changed. Plan again.",
            code="plan_changed",
        )
    store = records.AttemptStore.create(plan)
    events = EventEmitter(store.run_id, logger.on_event)
    artifact: Artifact | None = None
    checks: list[CheckObservation] = []
    stop_reason: str | None = None
    observed_models: tuple[str, ...] = ()
    deadline = time.monotonic() + plan.limits.attempt_seconds
    try:
        logger.attach_run(store.run_id, store.directory)
        events.emit(
            "selected",
            contract=str(plan.contract.path),
            harness=plan.harness,
            model=plan.model,
            run_directory=str(store.directory),
        )
        events.emit("phase", name="preflight")
        store.update("preflight", advisory_consent="flag")
        snapshot = workspace.capture_workspace(plan, store.directory)
        store.update("execution", baseline=snapshot)
        events.emit("phase", name="execution")
        runtime = RuntimeFactory.get_runtime_by_name(plan.harness, plan.model)
        request = runtime.build_contract_request(
            plan, snapshot, store.directory, timeout_seconds=_remaining(deadline)
        )
        request = replace(
            request, timeout_seconds=min(request.timeout_seconds, _remaining(deadline))
        )
        store.update("execution", native_controls=request.control_observations)
        decoder = ContractStreamDecoder(events, limits=plan.limits)
        producer = process.supervise_process(
            request,
            on_bytes=decoder.feed,
            on_started=lambda pid, pgid: store.update("execution", child_pid=pid, child_pgid=pgid),
            events=events,
            limits=plan.limits,
        )
        decoder.finish()
        observed_models = tuple(decoder.observed_models)
        store.update(
            "execution",
            producer=producer,
            observed_models=observed_models,
            native_reported_exit_code=decoder.native_exit_code,
        )
        stop_reason = _producer_failure(producer)
        if decoder.native_exit_code not in (None, 0):
            stop_reason = stop_reason or "native_reported_failure"
        if decoder.protocol_error:
            stop_reason = stop_reason or "native_protocol_error"
            events.emit(
                "diagnostic",
                severity="error",
                message=decoder.protocol_error,
                action="Inspect the retained native transcript before retrying.",
            )
        if not stop_reason and not decoder.completion_seen:
            stop_reason = "native_completion_unobserved"
        if stop_reason:
            events.emit(
                "diagnostic",
                severity="error",
                message=producer.error or f"Native execution did not complete: {stop_reason}.",
                action="Inspect the native transcript and execution prerequisites.",
            )
        if producer.cleanup_confirmed:
            if not stop_reason:
                _remaining(deadline)
            events.emit("phase", name="capture")
            store.update("capture")
            artifact = workspace.capture_output(
                snapshot, plan.contract.produces, store.directory, plan.limits
            )
            store.update("capture", artifact=artifact)
        if not stop_reason:
            if artifact is not None:
                events.emit("phase", name="checks")
                stop_reason = _run_checks(plan, snapshot, artifact, store, events, deadline, checks)
            else:
                events.emit(
                    "diagnostic",
                    severity="warning",
                    message=f"{plan.contract.produces} was not produced.",
                    action="Inspect the contract and retained Copilot transcript, then rerun.",
                )
    except KeyboardInterrupt:
        stop_reason = "cancelled"
        events.emit("stop_requested", reason="cancelled")
    except (ContractError, OSError) as exc:
        stop_reason = stop_reason or (
            exc.code if isinstance(exc, ContractError) else "filesystem_error"
        )
        events.emit(
            "diagnostic",
            severity="error",
            message=str(exc),
            action="Inspect the retained record and resolve the reported failure.",
        )
    events.emit("phase", name="record")
    result = RunResult(
        run_id=store.run_id,
        run_directory=store.directory,
        outcome=records.reduce_outcome(artifact, tuple(checks), stop_reason),
        artifact=artifact,
        checks=tuple(checks),
        stop_reason=stop_reason,
        requested_model=plan.model,
        observed_models=observed_models,
    )
    logger.close()
    store.update("record", transcript_retention=logger.transcript_metadata)
    store.finish(result)
    events.emit("finished", result=result)
    return result
