#!/usr/bin/env python3
"""Single-writer ledger for release-acceptance findings and coverage.

Subcommands (all print JSON to stdout, diagnostics to stderr):
  add       validate findings from a JSONL file and merge them into the
            ledger, deduplicating by fingerprint
  coverage  summarize coverage.csv rows per journey and status
  gate-row  compute the release-gate row from the ledger
  recheck   apply re-verification verdicts to open findings at a new SHA

The ledger is a JSON file: {"sha": ..., "findings": [...]}. Only the
orchestrator calls this script, so there is one writer.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

SEVERITIES = ("blocker", "should-fix", "nit")
STATUSES = ("open", "verified", "false-positive", "fixed", "still-open", "changed")
VERDICTS = ("VERIFIED-FIXED", "STILL-OPEN", "CHANGED", "FALSE-POSITIVE")
REQUIRED = ("title", "severity", "step", "observed", "expected", "repro", "sha")
COVERAGE_STATUSES = ("pass", "fail", "not-covered", "unreachable")


class LedgerError(ValueError):
    """Raised when a finding or verdict is malformed."""


def fingerprint(finding: dict) -> str:
    """Stable identity: step, rule or owner, and the normalized observed text."""
    basis = "|".join(
        [
            str(finding.get("step", "")).split(".")[0],
            str(finding.get("owner_file", "")).split(":")[0],
            " ".join(str(finding.get("observed", "")).lower().split())[:200],
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def validate(finding: dict) -> dict:
    missing = [key for key in REQUIRED if not finding.get(key)]
    if missing:
        raise LedgerError(f"finding {finding.get('title', '?')!r} missing {missing}")
    if finding["severity"] not in SEVERITIES:
        raise LedgerError(f"severity must be one of {SEVERITIES}")
    status = finding.setdefault("status", "open")
    if status not in STATUSES:
        raise LedgerError(f"status must be one of {STATUSES}")
    finding.setdefault("lifecycle_test", None)
    finding["fingerprint"] = fingerprint(finding)
    for key, value in finding.items():
        if isinstance(value, str) and not value.isascii():
            raise LedgerError(f"finding field {key!r} is not ASCII")
    return finding


def load_ledger(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"sha": None, "findings": []}


def add(ledger: dict, new: list[dict]) -> dict:
    index = {f["fingerprint"]: f for f in ledger["findings"]}
    added, duplicates = [], []
    for raw in new:
        finding = validate(dict(raw))
        if finding["fingerprint"] in index:
            duplicates.append(finding["fingerprint"])
            continue
        finding["id"] = f"RA-{len(ledger['findings']) + 1:03d}"
        ledger["findings"].append(finding)
        index[finding["fingerprint"]] = finding
        added.append(finding["id"])
    return {"added": added, "duplicates": duplicates}


def coverage(rows: list[dict]) -> dict:
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        status = row.get("status", "")
        base = status.split("(")[0]
        if base not in COVERAGE_STATUSES:
            raise LedgerError(f"coverage status {status!r} is not one of {COVERAGE_STATUSES}")
        journey = row.get("step", "").split(".")[0] or "?"
        bucket = summary.setdefault(journey, {key: 0 for key in COVERAGE_STATUSES})
        bucket[base] += 1
    totals = {key: sum(b[key] for b in summary.values()) for key in COVERAGE_STATUSES}
    probed = totals["pass"] + totals["fail"]
    planned = probed + totals["not-covered"]
    return {
        "per_journey": summary,
        "totals": totals,
        "probed_ratio": round(probed / planned, 3) if planned else 0.0,
    }


def gate_row(ledger: dict, sha: str) -> dict:
    """GO only when no blocker is open (verified or still-open) at this SHA."""
    open_states = {"open", "verified", "still-open", "changed"}
    blockers = [
        f["id"]
        for f in ledger["findings"]
        if f["severity"] == "blocker" and f["status"] in open_states
    ]
    unverified = [
        f["id"] for f in ledger["findings"] if f["status"] == "open" and f["severity"] == "blocker"
    ]
    without_test = [
        f["id"]
        for f in ledger["findings"]
        if f["status"] in {"verified", "still-open", "changed"} and not f.get("lifecycle_test")
    ]
    counts = {sev: 0 for sev in SEVERITIES}
    for finding in ledger["findings"]:
        if finding["status"] in open_states:
            counts[finding["severity"]] += 1
    verdict = "NO-GO" if blockers else "GO"
    return {
        "gate": "exploratory acceptance: no open verified blocker on the exact SHA",
        "sha": sha,
        "verdict": verdict,
        "open_blockers": blockers,
        "unverified_blockers": unverified,
        "open_counts": counts,
        "findings_without_lifecycle_test": without_test,
    }


def recheck(ledger: dict, verdicts: list[dict], sha: str) -> dict:
    by_id = {f["id"]: f for f in ledger["findings"]}
    table = []
    for item in verdicts:
        fid, verdict = item.get("id"), item.get("verdict")
        if fid not in by_id:
            raise LedgerError(f"unknown finding id {fid!r}")
        if verdict not in VERDICTS:
            raise LedgerError(f"verdict must be one of {VERDICTS}")
        finding = by_id[fid]
        finding["status"] = {
            "VERIFIED-FIXED": "fixed",
            "STILL-OPEN": "still-open",
            "CHANGED": "changed",
            "FALSE-POSITIVE": "false-positive",
        }[verdict]
        finding.setdefault("history", []).append(
            {"sha": sha, "verdict": verdict, "evidence": item.get("evidence", "")}
        )
        table.append(
            {
                "id": fid,
                "title": finding["title"],
                "verdict": verdict,
                "evidence": item.get("evidence", ""),
            }
        )
    ledger["sha"] = sha
    return {"sha": sha, "table": table}


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release-acceptance findings ledger.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_add = sub.add_parser("add", help="merge findings from a JSONL file")
    p_add.add_argument("--ledger", required=True)
    p_add.add_argument("--findings", required=True)
    p_cov = sub.add_parser("coverage", help="summarize a coverage CSV")
    p_cov.add_argument("--csv", required=True)
    p_gate = sub.add_parser("gate-row", help="compute the release-gate row")
    p_gate.add_argument("--ledger", required=True)
    p_gate.add_argument("--sha", required=True)
    p_re = sub.add_parser("recheck", help="apply verdicts at a new SHA")
    p_re.add_argument("--ledger", required=True)
    p_re.add_argument("--verdicts", required=True, help="JSONL of {id, verdict, evidence}")
    p_re.add_argument("--sha", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "coverage":
            with open(args.csv, encoding="utf-8", newline="") as handle:
                print(json.dumps(coverage(list(csv.DictReader(handle))), indent=2))
            return 0
        ledger_path = Path(args.ledger)
        ledger = load_ledger(ledger_path)
        if args.command == "add":
            result = add(ledger, _read_jsonl(Path(args.findings)))
        elif args.command == "gate-row":
            result = gate_row(ledger, args.sha)
        else:
            result = recheck(ledger, _read_jsonl(Path(args.verdicts)), args.sha)
    except (LedgerError, json.JSONDecodeError) as exc:
        print(f"findings: {exc}", file=sys.stderr)
        return 3
    if args.command in {"add", "recheck"}:
        ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
