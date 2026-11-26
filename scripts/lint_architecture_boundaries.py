#!/usr/bin/env python3
"""CLI entry point for the architecture boundary linter.

Wires :func:`scripts.architecture_linter.runner.run` to argv: prints sorted,
ASCII, Ruff-style ``path:line:column: rule-id message`` diagnostics for every
violation and every aggregated startup/read/parse/registry/rule failure, then
writes deterministic run metrics if `--metrics-json` was given. There is
deliberately no rule-selection flag here -- partial execution is a non-success
Python test API on `runner.run_selected_rules`, not a CLI surface, so every
invocation that prints findings always runs the full catalog.

A full run may use several worker processes (see
:mod:`scripts.architecture_linter.parallel`); the output and exit code are
identical to a single-process run. Internal, undocumented flags:

* ``--workers N`` overrides the worker count (also ``APM_ARCH_LINT_WORKERS``).
* ``--groups a,b --report-json PATH`` is the worker protocol: run only those
  rule groups, write a partial report to PATH, print nothing, and exit ``2``
  ("incomplete") so a worker can never masquerade as a clean full lint.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.architecture_linter import metrics as metrics_module  # noqa: E402
from scripts.architecture_linter import parallel, runner  # noqa: E402
from scripts.architecture_linter.diagnostics import (  # noqa: E402
    format_failure,
    render_violations_and_failures,
)
from scripts.architecture_linter.models import Failure  # noqa: E402


def _parse_groups(raw: str) -> tuple[str, ...]:
    names = tuple(name for name in raw.split(",") if name)
    unknown = sorted(set(names) - set(runner.GROUP_MODULE_NAMES))
    if not names or unknown or len(names) != len(set(names)):
        raise argparse.ArgumentTypeError(
            "expected a comma-separated subset of: " + ", ".join(runner.GROUP_MODULE_NAMES)
        )
    return names


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lint-architecture-boundaries",
        description="Enforce canonical-owner architecture boundaries across the repository.",
    )
    parser.add_argument(
        "--root",
        required=True,
        type=Path,
        help="Repository root to lint.",
    )
    parser.add_argument(
        "--metrics-json",
        required=False,
        type=Path,
        default=None,
        help="Path to write deterministic run metrics as JSON.",
    )
    parser.add_argument("--workers", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--groups", type=_parse_groups, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--report-json", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (args.groups is None) != (args.report_json is None):
        parser.error("--groups and --report-json must be given together")
    if args.groups is not None and (args.workers is not None or args.metrics_json is not None):
        parser.error("--groups runs one worker; it takes no --workers or --metrics-json")
    return args


def _run_worker(args: argparse.Namespace) -> int:
    partial = runner.run_groups(args.root, args.groups)
    try:
        parallel.write_partial_report(partial, args.report_json)
    except OSError as exc:
        print(format_failure(Failure("report", str(exc))), file=sys.stderr)
        return 1
    return parallel.PARTIAL_EXIT_CODE


def main(argv: list[str] | None = None) -> int:
    """Run the linter end to end and return a process exit code."""
    args = _parse_args(argv)
    if args.groups is not None:
        return _run_worker(args)

    try:
        workers = parallel.resolve_workers(args.workers, os.environ)
        timeout = parallel.resolve_timeout(os.environ)
    except parallel.WorkerConfigError as exc:
        print(format_failure(Failure("workers", str(exc))), file=sys.stderr)
        return 2

    if workers == 1:
        report = runner.run(args.root)
    else:
        report = parallel.run_parallel(args.root, parallel.plan_buckets(workers), timeout=timeout)

    rendered = render_violations_and_failures(report.violations, report.failures)
    if rendered:
        print(rendered)

    exit_code = report.exit_code

    if args.metrics_json is not None:
        try:
            metrics_module.write_metrics(report.metrics, args.metrics_json)
        except metrics_module.MetricsWriteError as exc:
            print(format_failure(Failure("metrics", str(exc))), file=sys.stderr)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
