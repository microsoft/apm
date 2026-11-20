#!/usr/bin/env python3
"""Single-writer ledger for release-acceptance findings and coverage.

Subcommands (all print JSON to stdout, diagnostics to stderr):
  add       validate findings from a JSONL file and merge them into the
            ledger, deduplicating by fingerprint (same owner and observed
            text across journeys is one finding)
  verify    apply P4 verifier verdicts (CONFIRMED, FALSE-POSITIVE, CHANGED)
  set       set route, pre_existing, lifecycle_test, owner_file or status
            on one finding (status known-issue removes it from the gate)
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
STATUSES = ("open", "verified", "false-positive", "fixed", "still-open", "changed", "known-issue")
VERIFY_VERDICTS = ("CONFIRMED", "FALSE-POSITIVE", "CHANGED")
SETTABLE = ("route", "pre_existing", "lifecycle_test", "owner_file", "owner_family", "status")
VERDICTS = ("VERIFIED-FIXED", "STILL-OPEN", "CHANGED", "FALSE-POSITIVE")
REQUIRED = ("title", "severity", "step", "observed", "expected", "repro", "sha")
COVERAGE_STATUSES = ("pass", "fail", "not-covered", "unreachable")


class LedgerError(ValueError):
    """Raised when a finding or verdict is malformed."""


def fingerprint(finding: dict) -> str:
    """Stable identity: owner file (or journey when unknown) plus observed text.

    The journey is not part of the identity when an owner is known, so one
    defect seen from two journeys deduplicates to one finding.
    """
    owner = str(finding.get("owner_file", "")).split(":")[0]
    anchor = owner or str(finding.get("step", "")).split(".")[0]
    observed = " ".join(str(finding.get("observed", "")).lower().split())[:200]
    return hashlib.sha256(f"{anchor}|{observed}".encode()).hexdigest()[:16]


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
        if ledger.get("sha") is None:
            ledger["sha"] = finding["sha"]
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
    """GO, NO-GO or PENDING-VERIFICATION for the exact SHA.

    NO-GO: a blocker that a verifier confirmed is still open (verified,
    still-open or changed). PENDING-VERIFICATION: no confirmed open blocker,
    but at least one blocker still awaits P4. Known issues never count.
    A ledger bound to another SHA cannot vouch for this one: the verdict is
    PENDING-VERIFICATION until a recheck moves the ledger to the SHA.
    """
    confirmed_open = {"verified", "still-open", "changed"}
    blockers = [
        f["id"]
        for f in ledger["findings"]
        if f["severity"] == "blocker" and f["status"] in confirmed_open
    ]
    unverified = [
        f["id"] for f in ledger["findings"] if f["severity"] == "blocker" and f["status"] == "open"
    ]
    without_test = [
        f["id"]
        for f in ledger["findings"]
        if f["status"] in confirmed_open and not f.get("lifecycle_test")
    ]
    counts = {sev: 0 for sev in SEVERITIES}
    for finding in ledger["findings"]:
        if finding["status"] in confirmed_open | {"open"}:
            counts[finding["severity"]] += 1
    stale = ledger.get("sha") not in (None, sha)
    if blockers and not stale:
        verdict = "NO-GO"
    elif unverified or stale:
        verdict = "PENDING-VERIFICATION"
    else:
        verdict = "GO"
    row = {
        "gate": "exploratory acceptance: no open verified blocker on the exact SHA",
        "sha": sha,
        "verdict": verdict,
        "open_blockers": blockers,
        "unverified_blockers": unverified,
        "open_counts": counts,
        "known_issues": [f["id"] for f in ledger["findings"] if f["status"] == "known-issue"],
        "findings_without_lifecycle_test": without_test,
    }
    if stale:
        row["stale_sha"] = {"ledger": ledger["sha"], "requested": sha}
    return row


def verify(ledger: dict, verdicts: list[dict]) -> dict:
    """Apply P4 verifier verdicts to open findings."""
    by_id = {f["id"]: f for f in ledger["findings"]}
    applied = []
    for item in verdicts:
        fid, verdict = item.get("id"), item.get("verdict")
        if fid not in by_id:
            raise LedgerError(f"unknown finding id {fid!r}")
        if verdict not in VERIFY_VERDICTS:
            raise LedgerError(f"verify verdict must be one of {VERIFY_VERDICTS}")
        finding = by_id[fid]
        finding["status"] = {
            "CONFIRMED": "verified",
            "FALSE-POSITIVE": "false-positive",
            "CHANGED": "changed",
        }[verdict]
        severity = item.get("severity")
        if severity:
            if severity not in SEVERITIES:
                raise LedgerError(f"severity must be one of {SEVERITIES}")
            finding["severity"] = severity
        entry = {"phase": "P4", "verdict": verdict, "rationale": item.get("rationale", "")}
        for key in ("observed", "repro"):
            value = item.get(key)
            if value and value != finding.get(key):
                if not str(value).isascii():
                    raise LedgerError(f"verifier field {key!r} is not ASCII")
                entry[f"walker_{key}"] = finding.get(key)
                finding[key] = value
        for key in ("pre_existing", "owner_file", "promise"):
            if key in item:
                finding[key] = item[key]
        finding.setdefault("history", []).append(entry)
        applied.append(
            {
                "id": fid,
                "verdict": verdict,
                "status": finding["status"],
                "severity": finding["severity"],
            }
        )
    return {"applied": applied}


def set_field(ledger: dict, fid: str, assignments: list[str]) -> dict:
    by_id = {f["id"]: f for f in ledger["findings"]}
    if fid not in by_id:
        raise LedgerError(f"unknown finding id {fid!r}")
    finding = by_id[fid]
    for assignment in assignments:
        key, sep, value = assignment.partition("=")
        if not sep or key not in SETTABLE:
            raise LedgerError(f"set expects KEY=VALUE with KEY in {SETTABLE}")
        if key == "status" and value not in STATUSES:
            raise LedgerError(f"status must be one of {STATUSES}")
        parsed: object = {"true": True, "false": False, "null": None}.get(value, value)
        if isinstance(parsed, str) and not parsed.isascii():
            raise LedgerError(f"value for {key!r} is not ASCII")
        finding[key] = parsed
    return {"id": fid, "finding": finding}


RECHECK_REQUIRED = ("open", "verified", "still-open", "changed")


def recheck(ledger: dict, verdicts: list[dict], sha: str) -> dict:
    """Re-verify every unresolved finding at ``sha`` and move the ledger to it.

    All verdicts are validated first; a missing verdict for any open,
    verified, still-open or changed finding refuses the whole recheck and
    leaves the ledger (including its SHA) untouched.
    """
    by_id = {f["id"]: f for f in ledger["findings"]}
    for item in verdicts:
        if item.get("id") not in by_id:
            raise LedgerError(f"unknown finding id {item.get('id')!r}")
        if item.get("verdict") not in VERDICTS:
            raise LedgerError(f"verdict must be one of {VERDICTS}")
    covered = {item["id"] for item in verdicts}
    missing = [
        f["id"]
        for f in ledger["findings"]
        if f["status"] in RECHECK_REQUIRED and f["id"] not in covered
    ]
    if missing:
        raise LedgerError(f"recheck at {sha} has no verdict for {missing}")
    table = []
    for item in verdicts:
        fid, verdict = item["id"], item["verdict"]
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
    p_ver = sub.add_parser("verify", help="apply P4 verifier verdicts")
    p_ver.add_argument("--ledger", required=True)
    p_ver.add_argument("--verdicts", required=True, help="JSONL of {id, verdict, severity?, ...}")
    p_set = sub.add_parser("set", help="set fields on one finding")
    p_set.add_argument("--ledger", required=True)
    p_set.add_argument("--id", required=True)
    p_set.add_argument("assignments", nargs="+", help="KEY=VALUE")
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
        if args.command != "add" and not ledger_path.is_file():
            print(f"findings: ledger not found: {ledger_path}", file=sys.stderr)
            return 2
        ledger = load_ledger(ledger_path)
        if args.command == "add":
            result = add(ledger, _read_jsonl(Path(args.findings)))
        elif args.command == "gate-row":
            result = gate_row(ledger, args.sha)
        elif args.command == "verify":
            result = verify(ledger, _read_jsonl(Path(args.verdicts)))
        elif args.command == "set":
            result = set_field(ledger, args.id, args.assignments)
        else:
            result = recheck(ledger, _read_jsonl(Path(args.verdicts)), args.sha)
    except (LedgerError, json.JSONDecodeError) as exc:
        print(f"findings: {exc}", file=sys.stderr)
        return 3
    if args.command in {"add", "recheck", "verify", "set"}:
        ledger_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
