#!/usr/bin/env python3
# ASCII-only. Pillar A: render the findings-ledger into the PR dossier.
"""Render a findings-ledger JSON file into the hardening dossier block.

This is the Pillar A deterministic TOOL BRIDGE (genesis S7): the PR
body's findings story is a pure FUNCTION of the persisted ledger, never
an LLM summary from recall. The orchestrator runs this script and hands
the stdout block to pr-description-skill, which MUST embed it verbatim
under the heading "## Hardening findings and resolution".

Input  : a ledger JSON file (schema in references/findings-ledger.md).
Output : the markdown dossier block on stdout.
Errors : diagnostics on stderr.

Exit codes:
  0 = dossier rendered
  2 = runner error (ledger missing, invalid JSON, schema mismatch)

The script is non-interactive and stdlib-only. Use --help for options.

    python scripts/render-dossier.py --ledger <path-to-ledger.json>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
HEADING = "## Hardening findings and resolution"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _load_ledger(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _log(f"[x] missing ledger: {path}")
        sys.exit(2)
    except json.JSONDecodeError as exc:
        _log(f"[x] invalid JSON in {path}: {exc}")
        sys.exit(2)
    if data.get("schema_version") != SCHEMA_VERSION:
        _log(
            f"[!] schema_version mismatch: ledger={data.get('schema_version')} "
            f"renderer={SCHEMA_VERSION}"
        )
    if not isinstance(data.get("findings"), list):
        _log("[x] ledger has no 'findings' array")
        sys.exit(2)
    return data


def _cell(value: Any) -> str:
    """Render one table cell, ASCII-safe, pipe-escaped, never blank."""
    if value is None or value == "":
        return "-"
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _trap_cell(finding: dict[str, Any]) -> str:
    trap = finding.get("trap_path")
    if not trap:
        return "-"
    kind = finding.get("test_kind")
    return f"{_cell(trap)} ({_cell(kind)})" if kind else _cell(trap)


def render(ledger: dict[str, Any]) -> str:
    findings = ledger["findings"]
    declined = [f for f in findings if f.get("charter_verdict") == "decline"]
    deferred = [f for f in findings if f.get("status") == "deferred"]
    # Accepted-and-resolved: charter accepted AND driven to fixed.
    # A deferred row (accepted but not yet folded) belongs only in the
    # deferred roll-up, not the resolution table.
    accepted = [
        f for f in findings if f.get("charter_verdict") == "accept" and f.get("status") == "fixed"
    ]

    lines: list[str] = [HEADING, ""]
    target = ledger.get("target")
    if target:
        lines.append(f"Target hardened: {_cell(target)}")
        lines.append("")

    lines.append(
        f"Adversarial sweep surfaced {len(findings)} finding(s): "
        f"{len(accepted)} accepted and fixed, {len(declined)} declined "
        f"as out-of-scope, {len(deferred)} deferred."
    )
    lines.append("")

    # Accepted + fixed table.
    lines.append("### Accepted findings and their resolution")
    lines.append("")
    if accepted:
        lines.append("| ID | Lens | Severity | Root cause | Fix | Regression trap |")
        lines.append("|----|------|----------|-----------|-----|-----------------|")
        for f in accepted:
            lines.append(
                f"| {_cell(f.get('id'))} | {_cell(f.get('lens'))} "
                f"| {_cell(f.get('severity'))} | {_cell(f.get('root_cause'))} "
                f"| {_cell(f.get('fix_commit'))} | {_trap_cell(f)} |"
            )
    else:
        lines.append("None.")
    lines.append("")

    # Declined roll-up (anti-scope-creep evidence).
    lines.append("### Declined as out-of-scope")
    lines.append("")
    if declined:
        lines.append("| ID | Lens | Severity | Charter clause | Vector |")
        lines.append("|----|------|----------|----------------|--------|")
        for f in declined:
            lines.append(
                f"| {_cell(f.get('id'))} | {_cell(f.get('lens'))} "
                f"| {_cell(f.get('severity'))} | {_cell(f.get('decline_clause'))} "
                f"| {_cell(f.get('root_cause') or f.get('vector'))} |"
            )
    else:
        lines.append("None.")
    lines.append("")

    # Deferred roll-up.
    lines.append("### Deferred")
    lines.append("")
    if deferred:
        lines.append("| ID | Lens | Severity | Reason |")
        lines.append("|----|------|----------|--------|")
        for f in deferred:
            lines.append(
                f"| {_cell(f.get('id'))} | {_cell(f.get('lens'))} "
                f"| {_cell(f.get('severity'))} | {_cell(f.get('root_cause'))} |"
            )
    else:
        lines.append("None.")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="render-dossier.py",
        description=(
            "Render a findings-ledger JSON file into the "
            "'## Hardening findings and resolution' markdown block "
            "(Pillar A, deterministic)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--ledger",
        required=True,
        help="Path to the findings-ledger JSON file.",
    )
    p.add_argument(
        "--out",
        default="-",
        help="Output path for the dossier block, or '-' for stdout (default).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ledger = _load_ledger(Path(args.ledger))
    block = render(ledger)
    if args.out == "-":
        sys.stdout.write(block)
    else:
        Path(args.out).write_text(block, encoding="utf-8")
        _log(f"[+] wrote dossier to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
