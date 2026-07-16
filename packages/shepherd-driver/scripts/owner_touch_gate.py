#!/usr/bin/env python3
"""Detect canonical-owner touches and verify shepherd completion evidence."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, overload

TABLE_START = "<!-- canonical-owner-table:v1 -->"
TABLE_END = "<!-- /canonical-owner-table -->"
TABLE_HEADER = ("Decision / fact", "Canonical owner", "Owner path selectors")
REPORT_VERSION = "1"
EVIDENCE_VERSION = "2"
DEFAULT_OWNER_TABLE = ".apm/instructions/architecture.instructions.md"
OWNER_CLASSIFICATIONS = {
    "owner-extension",
    "new-owner",
    "split-authority-repair",
}


class GateError(RuntimeError):
    """Raised when owner detection or evidence verification must fail closed."""


@dataclass(frozen=True)
class OwnerRow:
    """One parsed row from the canonical architecture owner table."""

    decision: str
    owner: str
    selectors: tuple[str, ...]


@overload
def _git(repo_root: Path, *args: str, text: Literal[True] = True) -> str: ...


@overload
def _git(repo_root: Path, *args: str, text: Literal[False]) -> bytes: ...


def _git(repo_root: Path, *args: str, text: bool = True) -> str | bytes:
    """Run git in repo_root and return stdout, raising GateError on failure."""
    command = ["git", "-C", str(repo_root), *args]
    completed = subprocess.run(  # noqa: S603 - fixed git executable, no shell
        command,
        check=False,
        capture_output=True,
        text=text,
    )
    if completed.returncode != 0:
        stderr = completed.stderr if text else completed.stderr.decode("utf-8", errors="replace")
        raise GateError(f"{' '.join(command)} failed: {stderr.strip()}")
    return completed.stdout


def _resolve_revision(repo_root: Path, revision: str) -> str:
    """Resolve revision to a full commit SHA."""
    resolved = _git(repo_root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    return resolved.strip()


def _owner_table_at_revision(
    repo_root: Path,
    revision_sha: str,
    owner_table: str,
) -> bytes:
    """Read the canonical owner table from an exact commit."""
    return _git(repo_root, "show", f"{revision_sha}:{owner_table}", text=False)


def _split_markdown_row(line: str) -> tuple[str, ...]:
    """Split a simple Markdown table row into stripped cells."""
    if not line.startswith("|") or not line.endswith("|"):
        raise GateError(f"malformed owner-table row: {line!r}")
    return tuple(cell.strip() for cell in line[1:-1].split("|"))


def _validate_selector(selector: str) -> None:
    """Reject selectors that are unsafe, ambiguous, or non-portable."""
    if not selector or not selector.isascii() or any(ord(char) < 32 for char in selector):
        raise GateError(f"invalid owner path selector: {selector!r}")
    path = PurePosixPath(selector)
    if path.is_absolute() or ".." in path.parts or selector.startswith("./"):
        raise GateError(f"owner path selector must be repository-relative: {selector!r}")
    if selector.endswith("/") or "\\" in selector:
        raise GateError(f"owner path selector must use a file-oriented POSIX pattern: {selector!r}")


def parse_owner_table(content: bytes) -> list[OwnerRow]:
    """Parse the one canonical owner table, rejecting any structural drift."""
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise GateError("canonical owner table must be printable ASCII") from exc

    lines = text.splitlines()
    if lines.count(TABLE_START) != 1 or lines.count(TABLE_END) != 1:
        raise GateError("canonical owner table markers are missing or duplicated")
    start = lines.index(TABLE_START)
    end = lines.index(TABLE_END)
    if end <= start + 2:
        raise GateError("canonical owner table is empty")

    table_lines = [line.strip() for line in lines[start + 1 : end] if line.strip()]
    if len(table_lines) < 3:
        raise GateError("canonical owner table has no data rows")
    if _split_markdown_row(table_lines[0]) != TABLE_HEADER:
        raise GateError("canonical owner table header drifted")

    delimiter = _split_markdown_row(table_lines[1])
    if len(delimiter) != len(TABLE_HEADER) or any(
        re.fullmatch(r":?-{3,}:?", cell) is None for cell in delimiter
    ):
        raise GateError("canonical owner table delimiter drifted")

    rows: list[OwnerRow] = []
    decisions: set[str] = set()
    selectors_seen: set[str] = set()
    for line in table_lines[2:]:
        cells = _split_markdown_row(line)
        if len(cells) != len(TABLE_HEADER):
            raise GateError(f"canonical owner table row has {len(cells)} cells, expected 3")
        decision, owner, selector_cell = cells
        if not decision or not owner:
            raise GateError("canonical owner table decision and owner must be non-empty")
        if decision in decisions:
            raise GateError(f"duplicate canonical owner decision: {decision}")

        selector_parts = selector_cell.split(";")
        if any(not part.strip() for part in selector_parts):
            raise GateError(f"canonical owner row has no selectors: {decision}")
        selectors_list: list[str] = []
        for part in selector_parts:
            selector = part.strip()
            if selector.startswith("`") or selector.endswith("`"):
                if not (selector.startswith("`") and selector.endswith("`") and len(selector) > 2):
                    raise GateError(f"malformed owner path selector: {selector!r}")
                selector = selector[1:-1]
            if "`" in selector:
                raise GateError(f"malformed owner path selector: {selector!r}")
            selectors_list.append(selector)
        selectors = tuple(selectors_list)
        for selector in selectors:
            _validate_selector(selector)
            if selector in selectors_seen:
                raise GateError(f"duplicate canonical owner selector: {selector}")
            selectors_seen.add(selector)

        decisions.add(decision)
        rows.append(OwnerRow(decision=decision, owner=owner, selectors=selectors))

    return rows


def _changed_files(repo_root: Path, base_sha: str, head_sha: str) -> list[str]:
    """Return every changed path, including both rename/copy endpoints."""
    output = _git(
        repo_root,
        "diff",
        "--name-status",
        "-z",
        base_sha,
        head_sha,
        text=False,
    )
    tokens = [token for token in output.split(b"\0") if token]
    paths: set[str] = set()
    index = 0
    while index < len(tokens):
        status = tokens[index].decode("ascii", errors="strict")
        index += 1
        path_count = 2 if status[0] in {"C", "R"} else 1
        if index + path_count > len(tokens):
            raise GateError("git diff emitted a truncated name-status record")
        for token in tokens[index : index + path_count]:
            try:
                paths.add(token.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise GateError("changed paths must be valid UTF-8") from exc
        index += path_count
    return sorted(paths)


def _tracked_files(repo_root: Path, head_sha: str) -> list[str]:
    """Return sorted repository-relative files present at the exact head."""
    output = _git(
        repo_root,
        "ls-tree",
        "-r",
        "--name-only",
        "-z",
        head_sha,
        text=False,
    )
    return sorted(path.decode("utf-8") for path in output.split(b"\0") if path)


def _validate_selector_matches(rows: list[OwnerRow], tracked_files: list[str]) -> None:
    """Reject owner selectors that cannot match any file at the exact head."""
    for row in rows:
        for selector in row.selectors:
            if not any(fnmatch.fnmatchcase(path, selector) for path in tracked_files):
                raise GateError(f"canonical owner selector matches no exact-head file: {selector}")


def _combined_owner_rows(
    base_rows: list[OwnerRow],
    head_rows: list[OwnerRow],
) -> list[OwnerRow]:
    """Combine exact-base and exact-head selectors without losing removed owners."""
    base_by_decision = {row.decision: row for row in base_rows}
    head_decisions = {row.decision for row in head_rows}
    combined: list[OwnerRow] = []
    for head_row in head_rows:
        base_row = base_by_decision.get(head_row.decision)
        selectors = tuple(
            dict.fromkeys((*(() if base_row is None else base_row.selectors), *head_row.selectors))
        )
        combined.append(
            OwnerRow(
                decision=head_row.decision,
                owner=head_row.owner,
                selectors=selectors,
            )
        )
    combined.extend(row for row in base_rows if row.decision not in head_decisions)
    return combined


def build_report(
    repo_root: Path,
    base: str,
    head: str,
    owner_table: str = DEFAULT_OWNER_TABLE,
) -> dict[str, Any]:
    """Build the deterministic owner-touch report for an exact revision pair."""
    base_sha = _resolve_revision(repo_root, base)
    head_sha = _resolve_revision(repo_root, head)
    base_table_content = _owner_table_at_revision(repo_root, base_sha, owner_table)
    head_table_content = _owner_table_at_revision(repo_root, head_sha, owner_table)
    base_rows = parse_owner_table(base_table_content)
    head_rows = parse_owner_table(head_table_content)
    _validate_selector_matches(base_rows, _tracked_files(repo_root, base_sha))
    _validate_selector_matches(head_rows, _tracked_files(repo_root, head_sha))
    owner_rows = _combined_owner_rows(base_rows, head_rows)
    changed_files = _changed_files(repo_root, base_sha, head_sha)

    touched_owners: list[dict[str, Any]] = []
    for row in owner_rows:
        matched_files = sorted(
            path
            for path in changed_files
            if any(fnmatch.fnmatchcase(path, selector) for selector in row.selectors)
        )
        if matched_files:
            touched_owners.append(
                {
                    "decision": row.decision,
                    "owner": row.owner,
                    "selectors": list(row.selectors),
                    "matched_files": matched_files,
                }
            )

    return {
        "version": REPORT_VERSION,
        "owner_table": owner_table,
        "owner_table_sha256": hashlib.sha256(head_table_content).hexdigest(),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "changed_files": changed_files,
        "touched_owners": touched_owners,
    }


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    """Return value as a mapping or fail closed with a field-specific error."""
    if not isinstance(value, dict):
        raise GateError(f"{field} must be an object")
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    """Return value as a list or fail closed with a field-specific error."""
    if not isinstance(value, list):
        raise GateError(f"{field} must be an array")
    return value


def verify_completion(
    completion: dict[str, Any],
    expected_report: dict[str, Any],
) -> dict[str, Any]:
    """Verify terminal functional evidence against a freshly derived report."""
    status = completion.get("status")
    terminal_statuses = {"ready-to-merge", "advisory-with-deferred"}
    if status in {"blocked", "superseded"}:
        return {
            "verified": True,
            "terminal_evidence_required": False,
            "status": status,
        }
    if status not in terminal_statuses:
        raise GateError(f"unsupported completion status: {status!r}")

    evidence = _require_mapping(completion.get("architecture_evidence"), "architecture_evidence")
    if evidence.get("version") != EVIDENCE_VERSION:
        raise GateError(f"architecture_evidence.version must be {EVIDENCE_VERSION!r}")

    embedded_report = _require_mapping(
        evidence.get("owner_touch_report"),
        "architecture_evidence.owner_touch_report",
    )
    if embedded_report != expected_report:
        raise GateError("owner_touch_report does not match fresh exact-head detection")

    touched_decisions = {item["decision"] for item in expected_report["touched_owners"]}
    classification = evidence.get("classification")
    if touched_decisions and classification not in OWNER_CLASSIFICATIONS:
        raise GateError(
            "classification self-exempts a deterministic owner touch; "
            "use owner-extension, new-owner, or split-authority-repair"
        )

    functional_tests = _require_list(
        evidence.get("functional_tests"),
        "architecture_evidence.functional_tests",
    )
    covered_decisions: set[str] = set()
    seen_test_ids: set[str] = set()
    expected_head = expected_report["head_sha"]
    for index, item in enumerate(functional_tests):
        test = _require_mapping(item, f"functional_tests[{index}]")
        test_id = test.get("test_id")
        if not isinstance(test_id, str) or not test_id.strip():
            raise GateError(f"functional_tests[{index}].test_id must be non-empty")
        if test_id in seen_test_ids:
            raise GateError(f"duplicate functional test id: {test_id}")
        seen_test_ids.add(test_id)
        if test.get("outcome") != "passed":
            raise GateError(f"functional test {test_id!r} did not pass")
        if test.get("head_sha") != expected_head:
            raise GateError(f"functional test {test_id!r} was not run at exact head")
        for field in ("command", "run_evidence"):
            value = test.get(field)
            if not isinstance(value, str) or not value.strip():
                raise GateError(f"functional test {test_id!r} has no {field}")

        owner_decisions = _require_list(
            test.get("owner_decisions"),
            f"functional_tests[{index}].owner_decisions",
        )
        for decision in owner_decisions:
            if not isinstance(decision, str) or not decision.strip():
                raise GateError(f"functional test {test_id!r} has an invalid owner decision")
            if decision not in touched_decisions:
                raise GateError(
                    f"functional test {test_id!r} cites untouched owner decision {decision!r}"
                )
            covered_decisions.add(decision)

    missing = sorted(touched_decisions - covered_decisions)
    if missing:
        raise GateError(
            "missing executed functional evidence for owner decisions: " + ", ".join(missing)
        )

    return {
        "verified": True,
        "terminal_evidence_required": True,
        "status": status,
        "owner_table_sha256": expected_report["owner_table_sha256"],
        "touched_owner_count": len(touched_decisions),
        "functional_test_ids": sorted(seen_test_ids),
    }


def _parser() -> argparse.ArgumentParser:
    """Build the non-interactive command-line interface."""
    parser = argparse.ArgumentParser(
        description=(
            "Detect canonical-owner touches from exact git revisions or verify "
            "a shepherd-driver completion return against fresh detection."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("detect", "verify"):
        child = subparsers.add_parser(command)
        child.add_argument("--repo-root", type=Path, default=Path("."))
        child.add_argument("--base", required=True, help="Exact base revision or commit SHA.")
        child.add_argument("--head", required=True, help="Exact head revision or commit SHA.")
        child.add_argument(
            "--owner-table",
            default=DEFAULT_OWNER_TABLE,
            help="Repository-relative canonical owner table path.",
        )
        if command == "verify":
            child.add_argument(
                "--completion",
                type=Path,
                required=True,
                help="Path to the completion return JSON.",
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run detection or verification with JSON stdout and diagnostics stderr."""
    args = _parser().parse_args(argv)
    try:
        report = build_report(
            args.repo_root.resolve(),
            args.base,
            args.head,
            args.owner_table,
        )
        if args.command == "detect":
            result = report
        else:
            completion = json.loads(args.completion.read_text(encoding="utf-8"))
            result = verify_completion(_require_mapping(completion, "completion"), report)
    except (GateError, OSError, json.JSONDecodeError) as exc:
        print(f"[x] owner-touch gate failed: {exc}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
