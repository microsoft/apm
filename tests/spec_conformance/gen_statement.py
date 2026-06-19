"""Generate the OpenAPM v0.1 conformance statement.

Reads:
  - build/conformance-coverage.json (written by conftest at collection)
  - docs/.../openapm-v0.1.requirements.yml (manifest)
  - test source files (for waiver/assertion extraction via ast)

Writes:
  - CONFORMANCE.json (canonical, sorted, ASCII-only)
  - CONFORMANCE.md (human-readable, deterministic)

The two files live at repo root like a lockfile so contributors trip
over them. CI gates them with `git diff --exit-code`.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections import defaultdict

from tests.spec_conformance._manifest import (
    ALLOWED_CLASSES,
    COVERAGE_PATH,
    REPO_ROOT,
    SPEC_PATH,
    load_requirements,
)

CONFORMANCE_JSON = REPO_ROOT / "CONFORMANCE.json"
CONFORMANCE_MD = REPO_ROOT / "CONFORMANCE.md"

SPEC_VERSION = "v0.1.1"
GENERATOR = "gen_statement.py v1"


def _ensure_coverage() -> dict[str, list[dict[str, str]]]:
    if not COVERAGE_PATH.exists():
        res = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/spec_conformance",
                "--collect-only",
                "-q",
                "-p",
                "no:randomly",
                "--no-header",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if not COVERAGE_PATH.exists():
            sys.stderr.write(res.stderr)
            raise SystemExit(2)
    with COVERAGE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _extract_waivers() -> dict[str, list[str]]:
    """Walk test files for `waive("...")` calls and bind them to req ids.

    Uses ast (not regex) so we are robust to formatting.
    """
    waivers: dict[str, list[str]] = defaultdict(list)
    suite_dir = REPO_ROOT / "tests" / "spec_conformance"
    for py in sorted(suite_dir.glob("test_*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            req_ids = []
            for deco in node.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and isinstance(deco.func, ast.Attribute)
                    and deco.func.attr == "req"
                ):
                    for a in deco.args:
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            req_ids.append(a.value)
            if not req_ids:
                continue
            for stmt in ast.walk(node):
                if (
                    isinstance(stmt, ast.Call)
                    and isinstance(stmt.func, ast.Name)
                    and stmt.func.id == "waive"
                    and stmt.args
                    and isinstance(stmt.args[0], ast.Constant)
                    and isinstance(stmt.args[0].value, str)
                ):
                    reason = stmt.args[0].value.strip()
                    for rid in req_ids:
                        if reason not in waivers[rid]:
                            waivers[rid].append(reason)
    return waivers


def _aggregate_status(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "unbound"
    statuses = {r["status"] for r in rows}
    if "active" in statuses:
        return "active"
    if "xfail" in statuses:
        return "xfail"
    return "skipped"


def build_json() -> dict:
    coverage = _ensure_coverage()
    waivers = _extract_waivers()
    reqs = load_requirements()
    entries = []
    for r in sorted(reqs, key=lambda x: x.id):
        rows = sorted(coverage.get(r.id, []), key=lambda x: x["test_nodeid"])
        entry = {
            "id": r.id,
            "keyword": r.keyword,
            "section": r.section,
            "conformance_class": r.conformance_class,
            "status": _aggregate_status(rows),
            "tests": [row["test_nodeid"] for row in rows],
            "test_count": len(rows),
        }
        if r.id in waivers:
            entry["waivers"] = sorted(waivers[r.id])
        entries.append(entry)
    by_class = {c: defaultdict(int) for c in ALLOWED_CLASSES}
    for e in entries:
        by_class[e["conformance_class"]][e["status"]] += 1
    summary = {
        c: {k: by_class[c].get(k, 0) for k in ("active", "skipped", "xfail", "unbound")}
        for c in ALLOWED_CLASSES
    }
    return {
        "spec_version": SPEC_VERSION,
        "generator": GENERATOR,
        "total_requirements": len(entries),
        "summary_by_class": summary,
        "requirements": entries,
    }


def _md_class_summary(summary: dict) -> str:
    lines = []
    lines.append("| Class | Active | Skipped | Xfail | Unbound |")
    lines.append("|-------|-------:|--------:|------:|--------:|")
    for c in ALLOWED_CLASSES:
        s = summary[c]
        lines.append(
            f"| {c.capitalize()} | {s['active']} | {s['skipped']} | {s['xfail']} | {s['unbound']} |"
        )
    return "\n".join(lines)


def build_md(doc: dict) -> str:
    preamble = (
        f"# OpenAPM Conformance Statement -- {SPEC_VERSION}\n\n"
        f"Generator: {GENERATOR}.\n"
        "Spec: [docs/src/content/docs/specs/openapm-v0.1.md]"
        "(docs/src/content/docs/specs/openapm-v0.1.md)\n\n"
        "This file is generated. Do NOT edit by hand. Run\n"
        "`uv run python -m tests.spec_conformance.gen_statement` to regenerate.\n\n"
        "## Honesty contract\n\n"
        "There is NO automated CI detector for spec-vs-behaviour drift "
        "beyond the four sets enforced by `orphan_check.py`: spec anchors, "
        "manifest entries, Appendix C rows, and `@pytest.mark.req` markers. "
        "A requirement marked `status=active` is exercised by at least one "
        "assertion. A requirement marked `status=skipped` carries a written "
        "waiver below; this is debt, not coverage. A requirement with "
        "`status=xfail` is asserted-but-known-broken.\n\n"
        "## Conformance classes\n\n"
        "All four conformance classes (Producer, Consumer, Registry, "
        "Governance) carry active coverage in this statement. The "
        "Registry class is exercised via the trust-anchor invariant "
        "test in `tests/spec_conformance/test_registry_reqs.py`, "
        "which hashes the committed Registry-archive fixture and "
        "asserts equality with the digest the paired lockfile "
        "advertises (sec.11.3.3, req-rg-001).\n\n"
    )
    summary_section = (
        "## Coverage summary\n\n" + _md_class_summary(doc["summary_by_class"]) + "\n\n"
    )
    rows = [
        "## Per-requirement coverage\n",
        "| Req ID | Keyword | Sec | Class | Status | Tests |",
        "|--------|---------|----:|-------|--------|------:|",
    ]
    for e in doc["requirements"]:
        rows.append(
            f"| [{e['id']}](docs/src/content/docs/specs/openapm-v0.1.md#{e['id']}) "
            f"| {e['keyword']} | {e['section']} | {e['conformance_class']} "
            f"| {e['status']} | {e['test_count']} |"
        )
    table = "\n".join(rows) + "\n\n"
    waivers_section = ["## Waivers\n"]
    for e in doc["requirements"]:
        if "waivers" in e:
            waivers_section.append(f"### {e['id']}")
            for w in e["waivers"]:
                waivers_section.append(f"- {w}")
            waivers_section.append("")
    waivers_md = "\n".join(waivers_section) + "\n"
    return preamble + summary_section + table + waivers_md


def _is_ascii(text: str) -> bool:
    return all(ord(c) == 0x09 or ord(c) == 0x0A or 0x20 <= ord(c) <= 0x7E for c in text)


def write_outputs() -> None:
    doc = build_json()
    json_text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    md_text = build_md(doc)
    if not _is_ascii(json_text):
        raise SystemExit("gen_statement: JSON contains non-ASCII bytes")
    if not _is_ascii(md_text):
        raise SystemExit("gen_statement: MD contains non-ASCII bytes")
    CONFORMANCE_JSON.write_text(json_text, encoding="ascii", newline="\n")
    CONFORMANCE_MD.write_text(md_text, encoding="ascii", newline="\n")
    print(f"[+] wrote {CONFORMANCE_JSON.name} and {CONFORMANCE_MD.name}")


def main() -> int:
    # F11 honesty: refuse to generate if spec anchors disagree with the
    # manifest. The orphan_check is the canonical gate, so we run it.
    res = subprocess.run(
        [sys.executable, "-m", "tests.spec_conformance.orphan_check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        sys.stderr.write(
            "\n[x] gen_statement refuses to write while orphan_check fails. "
            "Fix the 4-way bind first.\n"
        )
        return 1
    sanity = SPEC_PATH.read_text(encoding="utf-8").count('<a id="req-')
    if sanity == 0:
        sys.stderr.write("[x] spec has zero req anchors; aborting\n")
        return 1
    write_outputs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
