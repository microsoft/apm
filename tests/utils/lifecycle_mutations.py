"""Bounded production mutations, installed only inside disposable CLI processes.

Like scripts/run_mutation_pilot.py, reports are sorted, ASCII, atomic, and keep
errors separate from survivors. Its mutmut-specific exit-code table is NOT
reusable here: an arbitrary pytest/CLI failure is not evidence of a kill.
No source files, parent imports, or deployed artifacts are edited to inject a
fault. This wrapper exercises the installed Python entry point, not PyInstaller.
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LifecycleMutation:
    """One intentional behavioral change and its exact independent oracle."""

    id: str
    case_id: str
    owner: str
    intended_property: str
    failure_file: str
    failure_function: str
    failure_prefix: str
    witness_path: str


MUTATIONS = (
    LifecycleMutation(
        "wrong-target-deployment",
        "copilot-skills-project",
        "apm_cli.integration.skill_integrator.SkillIntegrator._promote_sub_skills",
        "routing.required_authorized_deployment",
        "lifecycle_interaction_oracle.py",
        "assert_routing",
        "Missing required deployment:",
        ".agents/skills/skills-copilot-skills-project/SKILL.md",
    ),
    LifecycleMutation(
        "omitted-ledger-claim",
        "copilot-skills-project",
        "apm_cli.core.deployment_ledger.DeploymentLedgerCodec.rows",
        "ownership.source_derived_claims",
        "lifecycle_interaction_oracle.py",
        "assert_routing",
        "Ledger differs from source-derived routing:",
        ".agents/skills/skills-copilot-skills-project/SKILL.md",
    ),
    LifecycleMutation(
        "skipped-target-cleanup",
        "copilot-prompt-widen-narrow",
        "apm_cli.integration.cleanup.remove_stale_deployed_files",
        "cleanup.removed_target_absent",
        "lifecycle_interaction_oracle.py",
        "assert_routing",
        "Removed target leaked deployment:",
        ".cursor/commands/prompts-copilot-prompt-widen-narrow.md",
    ),
)


def _append_event(path: Path, event: dict[str, Any]) -> None:
    """Append child-only reachability evidence outside the observed roots."""
    with path.open("a", encoding="ascii", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def _install_probe(
    mutation: LifecycleMutation, *, active: bool, emit: Callable[[dict[str, Any]], None]
) -> None:
    """Wrap precisely one production owner before CLI modules capture its name."""
    if mutation.id == "wrong-target-deployment":
        from apm_cli.integration.skill_integrator import SkillIntegrator
        from apm_cli.integration.targets import KNOWN_TARGETS

        original = SkillIntegrator._promote_sub_skills

        def promote_skills(
            source: Path, target_skills_root: Path, parent_name: str, **kwargs: Any
        ) -> tuple[int, list[Path]]:
            project_root = kwargs.get("project_root")
            reach = None
            if project_root is not None and (
                target_skills_root
                == SkillIntegrator._target_skills_root(KNOWN_TARGETS["copilot"], project_root)
            ):
                replacement = SkillIntegrator._target_skills_root(
                    KNOWN_TARGETS["claude"], project_root
                )
                reach = {
                    "event": "reach",
                    "original": target_skills_root.as_posix(),
                    "replacement": replacement.as_posix(),
                    "changed": active and replacement != target_skills_root,
                }
                if active:
                    target_skills_root = replacement
            result = original(source, target_skills_root, parent_name, **kwargs)
            if reach is not None:
                # Reconciliation can subsequently delete the wrong-target copy.
                # Prove the production writer really emitted it before observing
                # the missing-authorized-deployment law at the command boundary.
                written = [
                    path.as_posix()
                    for path in result[1]
                    if path.parent == target_skills_root and (path / "SKILL.md").is_file()
                ]
                emit({**reach, "written": written, "changed": reach["changed"] and bool(written)})
            return result

        SkillIntegrator._promote_sub_skills = staticmethod(promote_skills)
    elif mutation.id == "omitted-ledger-claim":
        from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

        original_rows = DeploymentLedgerCodec.rows

        def ledger_rows(ledger: Any) -> list[dict[str, Any]]:
            rows = original_rows(ledger)
            omitted = [row for row in rows if row["value"] == mutation.witness_path]
            if omitted:
                emit(
                    {
                        "event": "reach",
                        "paths": [row["value"] for row in omitted],
                        "changed": active,
                    }
                )
            return [row for row in rows if row not in omitted] if active else rows

        DeploymentLedgerCodec.rows = staticmethod(ledger_rows)
    elif mutation.id == "skipped-target-cleanup":
        from apm_cli.integration import cleanup

        original_cleanup = cleanup.remove_stale_deployed_files

        def clean_paths(stale_paths: Iterable[str], project_root: Path, **kwargs: Any) -> Any:
            paths = tuple(stale_paths)
            selected = mutation.witness_path in paths
            if selected:
                existed = (project_root / mutation.witness_path).is_file()
                emit(
                    {
                        "event": "reach",
                        "paths": [mutation.witness_path],
                        "existed": existed,
                        "changed": active and existed,
                    }
                )
            # Drop the actual deletion input, not the already-produced artifact.
            forwarded = (
                tuple(path for path in paths if path != mutation.witness_path) if active else paths
            )
            return original_cleanup(forwarded, project_root, **kwargs)

        cleanup.remove_stale_deployed_files = clean_paths
    else:
        raise ValueError(f"Unknown lifecycle mutation: {mutation.id}")


def child_main(arguments: list[str]) -> int:
    """Invoke the installed CLI after process-local mutation and reach logging."""
    mutant_id, mode, event_name, expected_source, *cli_arguments = arguments
    if mode not in {"baseline", "mutant"}:
        raise ValueError(f"Invalid campaign mode: {mode}")
    mutation = next(item for item in MUTATIONS if item.id == mutant_id)
    import apm_cli

    source = Path(apm_cli.__file__).resolve().parent
    if source != Path(expected_source).resolve():
        raise RuntimeError(f"Installed source mismatch: {source}")
    if "sitecustomize" not in sys.modules:
        raise RuntimeError("Isolated Python network guard was not loaded")
    distribution = importlib.metadata.distribution("apm-cli")
    entry = next(
        item
        for item in distribution.entry_points
        if item.group == "console_scripts" and item.name == "apm"
    )
    if entry.value != "apm_cli.cli:main":
        raise RuntimeError(f"Unexpected installed CLI entry point: {entry.value}")

    def emit(event: dict[str, Any]) -> None:
        _append_event(
            Path(event_name),
            {
                **event,
                "mutant_id": mutant_id,
                "mode": mode,
                "command": cli_arguments,
            },
        )

    emit(
        {
            "event": "command_start",
            "source": source.as_posix(),
            "entry_point": entry.value,
            "python": sys.executable,
        }
    )
    returncode = 1
    try:
        _install_probe(mutation, active=mode == "mutant", emit=emit)
        sys.argv = ["apm", *cli_arguments]
        try:
            value = entry.load()()
            returncode = value if isinstance(value, int) else 0
        except SystemExit as error:
            returncode = error.code if isinstance(error.code, int) else (1 if error.code else 0)
    finally:
        emit({"event": "command_end", "returncode": returncode})
    return returncode


@dataclass(frozen=True)
class RunObservation:
    """A clean execution or a specifically located parent-oracle failure."""

    outcome: str
    diagnostic: str
    failure_file: str
    failure_function: str
    elapsed_seconds: float
    events: tuple[dict[str, Any], ...]


def observe_run(action: Callable[[], object], events_path: Path) -> RunObservation:
    """Capture infrastructure errors without turning them into mutant kills."""
    started = time.monotonic()
    outcome, diagnostic, failure_file, failure_function = "executed", "", "", ""
    try:
        action()
    except Exception as error:
        outcome = "assertion" if isinstance(error, AssertionError) else "error"
        diagnostic = f"{type(error).__name__}: {error}"
        frames = traceback.extract_tb(error.__traceback__)
        if frames:
            failure_file = Path(frames[-1].filename).name
            failure_function = frames[-1].name
    try:
        events = (
            tuple(json.loads(line) for line in events_path.read_text(encoding="ascii").splitlines())
            if events_path.exists()
            else ()
        )
        if not all(isinstance(event, dict) for event in events):
            raise ValueError("Reach events must be objects")
    except (OSError, ValueError) as error:
        outcome, diagnostic, events = "error", f"Invalid reach evidence: {error}", ()
    return RunObservation(
        outcome,
        diagnostic,
        failure_file,
        failure_function,
        round(time.monotonic() - started, 6),
        events,
    )


def _commands_succeeded(
    observation: RunObservation, mutation: LifecycleMutation, mode: str
) -> bool:
    """Bind ordered, successful command events to this exact baseline or mutant."""
    active_command = None
    completed = 0
    for event in observation.events:
        if event.get("mutant_id") != mutation.id or event.get("mode") != mode:
            return False
        command = event.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(argument, str) for argument in command)
        ):
            return False
        kind = event.get("event")
        if kind == "command_start" and active_command is None:
            active_command = command
        elif kind == "reach" and active_command == command:
            if type(event.get("changed")) is not bool or (mode == "baseline" and event["changed"]):
                return False
        elif kind == "command_end" and active_command == command:
            if type(event.get("returncode")) is not int or event["returncode"] != 0:
                return False
            active_command = None
            completed += 1
        else:
            return False
    return completed > 0 and active_command is None


def _reached(mutation: LifecycleMutation, observation: RunObservation, *, active: bool) -> bool:
    """Require the mutation-specific effect witness, not just a generic reach flag."""
    for event in observation.events:
        if event.get("event") != "reach" or event.get("changed") is not active:
            continue
        if mutation.id == "wrong-target-deployment":
            original, replacement = event.get("original"), event.get("replacement")
            written = event.get("written")
            destination = replacement if active else original
            if (
                isinstance(original, str)
                and isinstance(replacement, str)
                and original != replacement
                and isinstance(written, list)
                and bool(written)
                and all(
                    isinstance(path, str) and Path(path).parent.as_posix() == destination
                    for path in written
                )
            ):
                return True
        else:
            paths = event.get("paths")
            if (
                isinstance(paths, list)
                and mutation.witness_path in paths
                and (mutation.id != "skipped-target-cleanup" or event.get("existed") is True)
            ):
                return True
    return False


def _intended_failure(mutation: LifecycleMutation, observation: RunObservation) -> bool:
    """Match the actual assertion and exact path, not a neighboring path prefix."""
    prefix = f"AssertionError: {mutation.failure_prefix}"
    first_line = observation.diagnostic.splitlines()[0] if observation.diagnostic else ""
    path_matches = (
        first_line.startswith(f"{prefix} missing=")
        and repr(mutation.witness_path) in first_line.partition(", unexpected=")[0]
        if mutation.id == "omitted-ledger-claim"
        else first_line == f"{prefix} {mutation.witness_path}"
    )
    return (
        observation.outcome == "assertion"
        and observation.failure_file == mutation.failure_file
        and observation.failure_function == mutation.failure_function
        and path_matches
    )


def classify_mutation(
    mutation: LifecycleMutation,
    baseline: RunObservation,
    mutant: RunObservation,
) -> dict[str, Any]:
    """Count only a reached fault and its exact law assertion after a green baseline."""
    baseline_reached = _reached(mutation, baseline, active=False)
    reached = _reached(mutation, mutant, active=True)
    baseline_green = (
        baseline.outcome == "executed"
        and baseline_reached
        and _commands_succeeded(baseline, mutation, "baseline")
    )
    status = "error"
    if baseline_green and reached and _commands_succeeded(mutant, mutation, "mutant"):
        if _intended_failure(mutation, mutant):
            status = "killed"
        elif mutant.outcome == "executed":
            status = "survived"
    return {
        "mutant_id": mutation.id,
        "case_id": mutation.case_id,
        "owner": mutation.owner,
        "intended_property": mutation.intended_property,
        "reached": reached,
        "baseline_reached": baseline_reached,
        "baseline_outcome": baseline.outcome,
        "baseline_green": baseline_green,
        "status": status,
        "failure_diagnostic": mutant.diagnostic,
        "baseline": asdict(baseline),
        "mutant": asdict(mutant),
    }


def mutation_report(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Compute the bounded campaign denominator independently of received records."""
    rows = sorted(results, key=lambda row: row["mutant_id"])
    ids = [row["mutant_id"] for row in rows]
    required = {mutation.id for mutation in MUTATIONS}
    if len(ids) != len(set(ids)) or set(ids) - required:
        raise ValueError("Unknown or duplicate lifecycle mutation evidence")
    if any(row["status"] not in {"killed", "survived", "error"} for row in rows):
        raise ValueError("Unknown lifecycle mutation status")
    return {
        "schema_version": 1,
        "campaign": "bounded-lifecycle-production-mutations",
        "execution_boundary": "installed-python-source-cli; not a frozen binary",
        "mutation_isolation": "child-process-runtime-only",
        "scope": "three hand-selected mutants; not overall Phase 2 completion",
        "unexecuted_mutant_ids": sorted(required - set(ids)),
        "counts": {
            **{
                status: sum(row["status"] == status for row in rows)
                for status in ("killed", "survived", "error")
            },
            "total": len(rows),
        },
        "results": rows,
    }


def write_report(
    path: Path,
    results: Iterable[dict[str, Any]],
    *,
    input_revision: str | None = None,
) -> dict[str, Any]:
    """Persist canonical evidence using the same atomic writer as the pilot."""
    from apm_cli.utils.atomic_io import atomic_write_text

    report = mutation_report(results)
    if input_revision is not None:
        report["input_revision"] = input_revision
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    atomic_write_text(path, content)
    return json.loads(content)


def _observation_from_mapping(payload: Any) -> RunObservation:
    """Validate serialized observations before recomputing a reported outcome."""
    if not isinstance(payload, dict):
        raise ValueError("Mutation observation must be an object")
    fields = ("outcome", "diagnostic", "failure_file", "failure_function")
    if not all(isinstance(payload.get(field), str) for field in fields):
        raise ValueError("Mutation observation text fields must be strings")
    elapsed, events = payload.get("elapsed_seconds"), payload.get("events")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (float, int))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise ValueError("Mutation duration must be finite and nonnegative")
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise ValueError("Mutation events must be an array of objects")
    return RunObservation(
        *(payload[field] for field in fields),
        float(elapsed),
        tuple(events),
    )


ISOLATION_CHECKS = (
    "parent_owners_unchanged",
    "parent_catalog_unchanged",
    "production_sources_unchanged",
)


def read_mutation_results(path: Path) -> tuple[dict[str, Any], ...]:
    """Recompute each JUnit claim, then let pytest and isolation failures veto it."""
    root = ET.parse(path).getroot()  # noqa: S314 - Local pytest-generated JUnit.
    results = []
    for case in root.iter("testcase"):
        properties = [
            prop.get("value")
            for prop in case.findall("./properties/property")
            if prop.get("name") == "lifecycle_mutation"
        ]
        if not properties:
            continue
        if len(properties) != 1 or properties[0] is None:
            raise ValueError("Ambiguous lifecycle mutation evidence")
        report = json.loads(properties[0])
        if not isinstance(report, dict) or not isinstance(report.get("results"), list):
            raise ValueError("Malformed lifecycle mutation report")
        if len(report["results"]) != 1 or not isinstance(report["results"][0], dict):
            raise ValueError("Each mutation test must supply exactly one result")
        payload = report["results"][0]
        mutation = next((item for item in MUTATIONS if item.id == payload.get("mutant_id")), None)
        if mutation is None:
            raise ValueError("Unknown lifecycle mutation")
        result = classify_mutation(
            mutation,
            _observation_from_mapping(payload.get("baseline")),
            _observation_from_mapping(payload.get("mutant")),
        )
        for field, expected in result.items():
            if field in {"baseline", "mutant"}:
                continue
            if type(payload.get(field)) is not type(expected) or payload[field] != expected:
                raise ValueError(f"Mutation evidence disagrees with observed {field}")
        for field in ISOLATION_CHECKS:
            if type(payload.get(field)) is not bool:
                raise ValueError(f"Missing or invalid mutation isolation check: {field}")
            result[field] = payload[field]
        canonical = mutation_report((result,))
        for field in (
            "schema_version",
            "campaign",
            "execution_boundary",
            "mutation_isolation",
            "scope",
            "counts",
        ):
            if type(report.get(field)) is not type(canonical[field]) or (
                report[field] != canonical[field]
            ):
                raise ValueError(f"Invalid mutation report {field}")
        if not all(type(value) is int for value in report["counts"].values()):
            raise ValueError("Mutation counts must be integers")
        outcome = next(
            (kind for kind in ("failure", "error", "skipped") if case.find(kind) is not None),
            "passed",
        )
        result["pytest_outcome"] = outcome
        if outcome != "passed" or not all(result[field] for field in ISOLATION_CHECKS):
            result["status"] = "error"
        results.append(result)
    return tuple(results)


def assert_complete_mutation_evidence(results: Iterable[dict[str, Any]]) -> None:
    """Require all three non-exempt, reached production mutants to be killed."""
    report = mutation_report(results)
    assert not report["unexecuted_mutant_ids"], (
        f"Missing lifecycle mutation evidence: {report['unexecuted_mutant_ids']}"
    )
    for row in report["results"]:
        assert (
            row["status"] == "killed"
            and row.get("pytest_outcome") == "passed"
            and all(row.get(field) is True for field in ISOLATION_CHECKS)
        ), f"Unmet lifecycle mutation obligation {row['mutant_id']}: {row['status']}"


if __name__ == "__main__":
    if sys.argv[1:2] != ["--child"]:
        raise SystemExit("This test helper only supports --child")
    raise SystemExit(child_main(sys.argv[2:]))
