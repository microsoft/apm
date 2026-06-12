#!/usr/bin/env python3
"""Render a coverage.json file as a Markdown summary.

Usage:
    python3 scripts/coverage-summary.py coverage.json [--title "My Title"]

Writes Markdown to stdout and, if GITHUB_STEP_SUMMARY is set, appends to
that file so the summary appears in the GitHub Actions job summary panel.

Exit codes:
    0  Always -- missing coverage.json is treated as a soft warning, not
       an error, so CI steps that set ``if: always()`` never fail here.
"""

import argparse
import json
import os
import pathlib
import sys


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a coverage.json file as a Markdown summary."
    )
    parser.add_argument(
        "coverage_json",
        metavar="COVERAGE_JSON",
        help="Path to coverage.json produced by `coverage json`.",
    )
    parser.add_argument(
        "--title",
        default="Code Coverage Report",
        help="Heading text for the summary (default: 'Code Coverage Report').",
    )
    return parser.parse_args()


def _build_markdown(data: dict, title: str) -> str:
    totals = data.get("totals", {})
    pct = totals.get("percent_covered_display", "N/A")
    stmts = totals.get("num_statements", 0)
    miss = totals.get("missing_lines", 0)
    covered = totals.get("covered_lines", stmts - miss)

    lines: list[str] = []
    lines.append(f"## {title}")
    lines.append("")
    lines.append(f"**Overall: {pct}%** ({covered:,}/{stmts:,} statements)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Statements | {stmts:,} |")
    lines.append(f"| Covered | {covered:,} |")
    lines.append(f"| Missed | {miss:,} |")
    lines.append(f"| Coverage | {pct}% |")
    lines.append("")

    files = data.get("files", {})
    ranked = sorted(
        files.items(),
        key=lambda kv: kv[1].get("summary", {}).get("percent_covered", 100.0),
    )
    # Always show bottom-10 files so the section appears even when
    # overall coverage is high (acceptance criteria: "collapsible
    # lowest-coverage files" in every summary).
    bottom = ranked[:10]

    if bottom:
        lines.append("<details>")
        lines.append("<summary>Lowest-coverage files</summary>")
        lines.append("")
        lines.append("| File | Stmts | Miss | Cover |")
        lines.append("|------|-------|------|-------|")
        for fpath, fdata in bottom:
            s = fdata.get("summary", {})
            # Strip common prefix to keep the table narrow.
            short = fpath.replace("src/apm_cli/", "")
            fp = s.get("percent_covered_display", "?")
            lines.append(
                f"| `{short}` | {s.get('num_statements', 0)}"
                f" | {s.get('missing_lines', 0)} | {fp}% |"
            )
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> None:
    args = _parse_args()
    coverage_path = pathlib.Path(args.coverage_json)

    if not coverage_path.exists():
        print(
            f"[!] coverage-summary: {coverage_path} not found -- skipping summary.",
            file=sys.stderr,
        )
        sys.exit(0)

    try:
        data = json.loads(coverage_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"[x] coverage-summary: failed to read {coverage_path}: {exc}",
            file=sys.stderr,
        )
        sys.exit(0)

    md = _build_markdown(data, args.title)
    print(md, end="")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write(md)
        except OSError as exc:
            print(
                f"[!] coverage-summary: could not write to GITHUB_STEP_SUMMARY: {exc}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
