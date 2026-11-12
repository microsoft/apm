#!/usr/bin/env python3
"""CLI entry point for the single-process architecture boundary linter.

Wires :func:`scripts.architecture_linter.runner.run` to argv: prints sorted,
ASCII, Ruff-style ``path:line:column: rule-id message`` diagnostics for every
violation and every aggregated startup/read/parse/registry/rule failure, then
writes deterministic run metrics if `--metrics-json` was given. There is
deliberately no rule-selection flag here -- partial execution is a non-success
Python test API on `runner.run_selected_rules`, not a CLI surface, so every
invocation of this script always runs the full catalog.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.architecture_linter import metrics as metrics_module  # noqa: E402
from scripts.architecture_linter import runner  # noqa: E402
from scripts.architecture_linter.diagnostics import (  # noqa: E402
    format_failure,
    render_violations_and_failures,
)
from scripts.architecture_linter.models import Failure  # noqa: E402


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the linter end to end and return a process exit code."""
    args = _parse_args(argv)

    report = runner.run(args.root)

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
