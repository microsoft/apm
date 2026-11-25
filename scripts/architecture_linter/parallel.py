"""Process-parallel driver for the architecture linter CLI.

The linter's hermeticity guard forbids child processes *inside* a run, so
parallelism lives outside it: this driver starts one CLI worker process per
bucket of rule groups (``--groups ... --report-json ...``), each worker runs
an ordinary single-process :func:`runner.run_groups`, and the driver merges
the workers' partial reports with :func:`runner.merge_partial_runs` -- the
same merge the single-process path uses -- so rendered findings and the exit
code are byte-identical to a one-process run.

The driver fails closed: a worker that exits with anything but the partial-run
code ``2``, is killed by a signal, times out, or leaves a missing or invalid
report fails the whole run with a ``parallel:<bucket>`` failure naming the
bucket. It never returns fewer findings than a one-process run would.

Worker count: ``--workers`` wins, then ``APM_ARCH_LINT_WORKERS``; otherwise 1
inside pytest (so in-process test seams and xdist shards stay single-process)
and ``min(4, cpu_count // 2)`` elsewhere, leaving half of a shared host free.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.architecture_linter.models import (
    Failure,
    GroupResult,
    PartialRun,
    RuleResult,
    RunMetrics,
    RunReport,
    Violation,
)
from scripts.architecture_linter.runner import (
    GROUP_MODULE_NAMES,
    empty_metrics,
    merge_partial_runs,
)

WORKERS_ENV = "APM_ARCH_LINT_WORKERS"
TIMEOUT_ENV = "APM_ARCH_LINT_WORKER_TIMEOUT"
DEFAULT_TIMEOUT_SECONDS = 900.0
MAX_DEFAULT_WORKERS = 4
PARTIAL_EXIT_CODE = 2
REPORT_VERSION = 1

# Marginal seconds per group in a fresh process (one CI-shaped run, minus the
# shared startup). Used only to balance buckets; a stale table costs wall
# time, never correctness.
GROUP_COST_SECONDS: Mapping[str, float] = {
    "contracts_tests": 26.0,
    "install_deployment": 15.0,
    "transport_platform": 9.0,
    "registry_delegation": 4.0,
    "marketplace_integrations": 3.0,
    "mutation_writes": 3.0,
}

_CLI_PATH = Path(__file__).resolve().parents[1] / "lint_architecture_boundaries.py"


class WorkerConfigError(ValueError):
    """Raised for an invalid worker-count flag or environment value."""


class ReportFormatError(ValueError):
    """Raised when a worker's partial report is missing a field or malformed."""


def _positive_int(raw: str, source: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise WorkerConfigError(f"{source} must be a positive integer, got {raw!r}") from None
    if value < 1:
        raise WorkerConfigError(f"{source} must be a positive integer, got {raw!r}")
    return value


def resolve_workers(flag: int | None, env: Mapping[str, str]) -> int:
    """Return the worker count, capped at the number of rule groups."""
    if flag is not None:
        value = _positive_int(str(flag), "--workers")
    elif env.get(WORKERS_ENV, "").strip():
        value = _positive_int(env[WORKERS_ENV].strip(), WORKERS_ENV)
    elif env.get("PYTEST_CURRENT_TEST") or env.get("PYTEST_XDIST_WORKER"):
        value = 1
    else:
        value = max(1, min(MAX_DEFAULT_WORKERS, (os.cpu_count() or 1) // 2))
    return min(value, len(GROUP_MODULE_NAMES))


def resolve_timeout(env: Mapping[str, str]) -> float:
    """Return the per-run worker timeout in seconds."""
    raw = env.get(TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    return float(_positive_int(raw, TIMEOUT_ENV))


def plan_buckets(workers: int) -> tuple[tuple[str, ...], ...]:
    """Split the groups into at most `workers` buckets, longest-first."""
    count = max(1, min(workers, len(GROUP_MODULE_NAMES)))
    loads = [0.0] * count
    members: list[list[str]] = [[] for _ in range(count)]
    by_cost = sorted(
        GROUP_MODULE_NAMES, key=lambda name: (-GROUP_COST_SECONDS.get(name, 1.0), name)
    )
    for name in by_cost:
        target = min(range(count), key=lambda index: (loads[index], index))
        loads[target] += GROUP_COST_SECONDS.get(name, 1.0)
        members[target].append(name)
    order = {name: index for index, name in enumerate(GROUP_MODULE_NAMES)}
    buckets = [tuple(sorted(bucket, key=order.__getitem__)) for bucket in members if bucket]
    return tuple(sorted(buckets, key=lambda bucket: order[bucket[0]]))


# --------------------------------------------------------------------------
# Partial-report JSON codec.
# --------------------------------------------------------------------------


def _violation_json(violation: Violation) -> list[object]:
    return [violation.rule_id, violation.path, violation.line, violation.column, violation.message]


def _failure_json(failure: Failure) -> list[str]:
    return [failure.stage, failure.message]


def _group_json(result: GroupResult) -> dict[str, object]:
    return {
        "group": result.group,
        "module_name": result.module_name,
        "import_error": result.import_error,
        "duration_seconds": result.duration_seconds,
        "rules": [
            {
                "rule_id": rule.rule_id,
                "group": rule.group,
                "guard_ids": list(rule.guard_ids),
                "violations": [_violation_json(v) for v in rule.violations],
                "error": rule.error,
            }
            for rule in result.rules
        ],
    }


def partial_to_json(partial: PartialRun) -> dict[str, object]:
    """Return a JSON-serializable document for one worker's partial run."""
    metrics = {
        name: getattr(partial.metrics, name)
        for name in RunMetrics.__dataclass_fields__
        if name != "per_group_seconds"
    }
    metrics["per_group_seconds"] = [list(pair) for pair in partial.metrics.per_group_seconds]
    return {
        "version": REPORT_VERSION,
        "groups": list(partial.groups),
        "rule_selection": partial.rule_selection,
        "inventory_ok": partial.inventory_ok,
        "inventory_digest": partial.inventory_digest,
        "startup_failures": [_failure_json(f) for f in partial.startup_failures],
        "violations": [_violation_json(v) for v in partial.violations],
        "rule_failures": [_failure_json(f) for f in partial.rule_failures],
        "io_failures": [_failure_json(f) for f in partial.io_failures],
        "group_results": [_group_json(result) for result in partial.group_results],
        "guard_hits": [[guard, count] for guard, count in partial.guard_hits],
        "registered_guard_ids": (
            None if partial.registered_guard_ids is None else list(partial.registered_guard_ids)
        ),
        "metrics": metrics,
    }


def write_partial_report(partial: PartialRun, path: Path) -> None:
    """Write `partial` as ASCII JSON to `path`; raises OSError on failure."""
    path.write_text(json.dumps(partial_to_json(partial), ensure_ascii=True), encoding="ascii")


def _expect(value: Any, kind: type | tuple[type, ...], what: str) -> Any:
    if isinstance(value, bool) and kind in (int, float, (int, float)):
        raise ReportFormatError(f"{what} must be a number")
    if not isinstance(value, kind):
        raise ReportFormatError(f"{what} has the wrong type")
    return value


def _str_list(value: object, what: str) -> tuple[str, ...]:
    items = _expect(value, list, what)
    return tuple(str(_expect(item, str, what)) for item in items)


def _decode_failures(value: object, what: str) -> tuple[Failure, ...]:
    failures = []
    for item in _expect(value, list, what):
        pair = _expect(item, list, what)
        if len(pair) != 2:
            raise ReportFormatError(f"{what} entries must be [stage, message]")
        stage, message = pair
        failures.append(Failure(str(_expect(stage, str, what)), str(_expect(message, str, what))))
    return tuple(failures)


def _decode_violations(value: object, what: str) -> tuple[Violation, ...]:
    violations = []
    for item in _expect(value, list, what):
        row = _expect(item, list, what)
        if len(row) != 5:
            raise ReportFormatError(f"{what} entries must have five fields")
        rule_id, path, line, column, message = row
        violations.append(
            Violation(
                str(_expect(rule_id, str, what)),
                str(_expect(path, str, what)),
                int(_expect(line, int, what)),
                int(_expect(column, int, what)),
                str(_expect(message, str, what)),
            )
        )
    return tuple(violations)


def _optional_str(value: object, what: str) -> str | None:
    return None if value is None else str(_expect(value, str, what))


def _decode_group(item: object) -> GroupResult:
    data = _expect(item, dict, "group_results")
    rules = []
    for raw_rule in _expect(data["rules"], list, "group rules"):
        rule = _expect(raw_rule, dict, "group rules")
        rules.append(
            RuleResult(
                rule_id=str(_expect(rule["rule_id"], str, "rule_id")),
                group=str(_expect(rule["group"], str, "rule group")),
                guard_ids=_str_list(rule["guard_ids"], "guard_ids"),
                violations=_decode_violations(rule["violations"], "rule violations"),
                error=_optional_str(rule["error"], "rule error"),
            )
        )
    return GroupResult(
        group=str(_expect(data["group"], str, "group")),
        module_name=str(_expect(data["module_name"], str, "module_name")),
        import_error=_optional_str(data["import_error"], "import_error"),
        rules=tuple(rules),
        duration_seconds=float(_expect(data["duration_seconds"], (int, float), "duration")),
    )


def _decode_metrics(value: object) -> RunMetrics:
    data = _expect(value, dict, "metrics")
    fields: dict[str, object] = {}
    for name, field in RunMetrics.__dataclass_fields__.items():
        raw = data[name]
        if name == "per_group_seconds":
            pairs = []
            for pair in _expect(raw, list, name):
                group, seconds = _expect(pair, list, name)
                pairs.append(
                    (str(_expect(group, str, name)), float(_expect(seconds, (int, float), name)))
                )
            fields[name] = tuple(pairs)
        elif field.type == "float":
            fields[name] = float(_expect(raw, (int, float), name))
        else:
            fields[name] = int(_expect(raw, int, name))
    return RunMetrics(**fields)


def partial_from_json(document: object) -> PartialRun:
    """Decode and strictly validate one worker's partial-run document."""
    try:
        data = _expect(document, dict, "report")
        if data.get("version") != REPORT_VERSION:
            raise ReportFormatError("unsupported report version")
        guard_hits = []
        for pair in _expect(data["guard_hits"], list, "guard_hits"):
            guard, count = _expect(pair, list, "guard_hits")
            guard_hits.append(
                (str(_expect(guard, str, "guard_hits")), int(_expect(count, int, "guard_hits")))
            )
        registered = data["registered_guard_ids"]
        return PartialRun(
            groups=_str_list(data["groups"], "groups"),
            rule_selection=bool(_expect(data["rule_selection"], bool, "rule_selection")),
            inventory_ok=bool(_expect(data["inventory_ok"], bool, "inventory_ok")),
            inventory_digest=str(_expect(data["inventory_digest"], str, "inventory_digest")),
            startup_failures=_decode_failures(data["startup_failures"], "startup_failures"),
            violations=_decode_violations(data["violations"], "violations"),
            rule_failures=_decode_failures(data["rule_failures"], "rule_failures"),
            io_failures=_decode_failures(data["io_failures"], "io_failures"),
            group_results=tuple(
                _decode_group(item)
                for item in _expect(data["group_results"], list, "group_results")
            ),
            guard_hits=tuple(guard_hits),
            registered_guard_ids=None
            if registered is None
            else _str_list(registered, "registered_guard_ids"),
            metrics=_decode_metrics(data["metrics"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ReportFormatError):
            raise
        raise ReportFormatError(f"malformed report: {type(exc).__name__}: {exc}") from None


# --------------------------------------------------------------------------
# Driver.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Worker:
    bucket: tuple[str, ...]
    report_path: Path
    stderr_path: Path
    process: subprocess.Popen[bytes]

    @property
    def label(self) -> str:
        return "+".join(self.bucket)


def worker_argv(root: Path, bucket: Sequence[str], report_path: Path) -> list[str]:
    """Return the argv for one worker process (a test seam for fault injection)."""
    return [
        sys.executable,
        str(_CLI_PATH),
        "--root",
        str(root),
        "--groups",
        ",".join(bucket),
        "--report-json",
        str(report_path),
    ]


def _wait_all(workers: Sequence[_Worker], timeout: float) -> set[str]:
    deadline = time.monotonic() + timeout
    timed_out: set[str] = set()
    for worker in workers:
        try:
            worker.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out.add(worker.label)
    for worker in workers:
        if worker.process.poll() is None:
            worker.process.kill()
            worker.process.wait()
    return timed_out


def _collect(worker: _Worker, timed_out: set[str], timeout: float) -> PartialRun | str:
    code = worker.process.returncode
    if worker.label in timed_out:
        return f"timed out after {timeout:g} s"
    if code < 0:
        return f"killed by signal {-code}"
    if code != PARTIAL_EXIT_CODE:
        return f"exited with code {code} (expected {PARTIAL_EXIT_CODE})"
    try:
        text = worker.report_path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        return f"report missing or unreadable: {type(exc).__name__}"
    try:
        partial = partial_from_json(json.loads(text))
    except (ValueError, ReportFormatError) as exc:
        return f"report invalid: {exc}"
    if partial.groups != worker.bucket:
        return "report covers groups " + ",".join(partial.groups)
    return partial


def _failed_report(failures: Sequence[Failure], seconds: float) -> RunReport:
    return RunReport(
        violations=(),
        failures=tuple(sorted(failures, key=lambda f: (f.stage, f.message))),
        group_results=(),
        metrics=empty_metrics(0, 0, seconds, 0),
        exit_code=1,
    )


def run_parallel(
    root: Path,
    buckets: Sequence[Sequence[str]],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> RunReport:
    """Lint `root` with one worker process per bucket and merge the partials."""
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="apm-arch-lint-") as scratch:
        scratch_dir = Path(scratch)
        workers: list[_Worker] = []
        for index, bucket in enumerate(buckets):
            report_path = scratch_dir / f"bucket-{index}.json"
            stderr_path = scratch_dir / f"bucket-{index}.stderr"
            with stderr_path.open("wb") as stderr_handle:
                process = subprocess.Popen(  # noqa: S603 - argv is this CLI plus fixed flags
                    worker_argv(root, bucket, report_path),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_handle,
                )
            workers.append(_Worker(tuple(bucket), report_path, stderr_path, process))
        timed_out = _wait_all(workers, timeout)
        partials: list[PartialRun] = []
        failures: list[Failure] = []
        for worker in workers:
            outcome = _collect(worker, timed_out, timeout)
            if isinstance(outcome, PartialRun):
                partials.append(outcome)
                continue
            failures.append(Failure(f"parallel:{worker.label}", outcome))
            sys.stderr.write(worker.stderr_path.read_text(encoding="utf-8", errors="replace"))
    seconds = time.perf_counter() - start
    if failures:
        return _failed_report(failures, seconds)
    return merge_partial_runs(partials, total_seconds=seconds)
