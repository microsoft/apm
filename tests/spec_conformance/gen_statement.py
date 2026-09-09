"""Generate the selected OpenAPM revision's static binding inventory.

Reads:
  - a fresh, version-stamped full-suite collection
  - the selected requirements manifest
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
import sys
from collections import defaultdict

from tests.spec_conformance._manifest import (
    ALLOWED_CLASSES,
    REPO_ROOT,
    Coverage,
    collect_coverage,
    load_requirements,
    selected_assessment,
)
from tests.spec_conformance.orphan_check import check_bindings

CONFORMANCE_JSON = REPO_ROOT / "CONFORMANCE.json"
CONFORMANCE_MD = REPO_ROOT / "CONFORMANCE.md"

GENERATOR = "gen_statement.py v2"
USER_SCOPE_DISCLOSURE = {
    "manifest_location": "~/.apm/apm.yml",
    "lockfile_location": "~/.apm/apm.lock.yaml",
    "target_capability_declaration": (
        "MCPClientAdapter.supports_user_scope (OpenAPM Target Registry v0.1 implementation profile)"
    ),
}
ASSESSMENT_LIMITATIONS = [
    "The reference CLI's bare content audit uses source-derived drift replay, not the "
    "stored-hash baseline required by req-lk-017's unqualified audit obligation. "
    "The stored-hash and full-SHA consistency baselines are exercised in CI/conformance "
    "audit. This inventory does not claim full Consumer conformance in bare audit mode.",
    "The native Cowork audit controls use a controlled pre-existing standalone-skill "
    "snapshot; they do not establish a successful Cowork install/audit round trip. "
    "The Grok user-scope control does exercise install and audit.",
    "A selected native runtime without an isolated replay backend is reported as "
    "unsupported before its live writer. No new native database scratch backend "
    "or hosted-runtime evidence is supplied by this inventory.",
    "A source-only coupled probe shows that local acquisition dereferences an admitted "
    "internal resource symlink, but inherited replay plans from the original source "
    "representation and can falsely report the deployed regular file as orphaned "
    "during unchanged CI audit. The narrowly expected-failing regression separately "
    "verifies content integrity and unchanged live state; an escaping-link refusal "
    "control remains unsuppressed. This is a replay limitation, not evidence of an "
    "escape, external-file read or security bypass.",
    "The retained manifest schema rejects git entries with a path modifier and id "
    "entries with an explicit registry modifier. It also accepts malformed or "
    "wrong-length policy.hash strings structurally. Schema acceptance is not evidence "
    "of Consumer digest-envelope enforcement under req-mf-018 or req-lk-016.",
    "Inherited Git-tree boundaries for symlink blobs, gitlinks/submodules and "
    "CRLF/LFS-filtered checkout bytes lack cross-platform execution evidence in this "
    "assessment. The req-lk-015 obligation and digest construction remain unchanged.",
]


def _ensure_coverage() -> Coverage:
    """Require fresh collection and exact four-way binding before rendering."""
    coverage = collect_coverage()
    if check_bindings(coverage):
        raise ValueError("gen_statement refuses to write while the four-way bind fails")
    return coverage


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
    assessment = selected_assessment()
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
        if r.fixture is not None:
            entry["fixture"] = r.fixture
        if r.oracle is not None:
            entry["oracle"] = r.oracle
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
        **assessment.stamp(),
        "spec_path": assessment.spec_path.relative_to(REPO_ROOT).as_posix(),
        "spec_citation": assessment.citation,
        "generator": GENERATOR,
        "assessment_status": "DRAFT",
        "human_ratification": "UNSATISFIED",
        "activation": "UNSATISFIED",
        "total_requirements": len(entries),
        "summary_by_class": summary,
        "consumer_user_scope": USER_SCOPE_DISCLOSURE,
        "assessment_limitations": ASSESSMENT_LIMITATIONS,
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
        f"# OpenAPM Conformance Binding Inventory -- {doc['spec_version']} (DRAFT)\n\n"
        f"Generator: {GENERATOR}.\n"
        f"Spec: [{doc['spec_path']}]({doc['spec_path']})\n"
        f"Exact revision citation: {doc['spec_citation']}\n\n"
        "This file is generated. Do NOT edit by hand. Run\n"
        "`uv run python -m tests.spec_conformance.gen_statement` to regenerate.\n\n"
        "## Honesty contract\n\n"
        "There is NO automated CI detector for spec-vs-behaviour drift "
        "beyond the four sets enforced by `orphan_check.py`: spec anchors, "
        "manifest entries, Appendix C rows, and `@pytest.mark.req` markers. "
        "Statuses are a static binding inventory from fresh full-suite collection, "
        "not executed test results or a runtime pass certificate. `status=active` "
        "means a collected binding is not statically marked skipped or xfail; "
        "it does not prove that an assertion ran or passed. `status=skipped` "
        "and `status=xfail` describe static markers or waiver calls, not measured "
        "execution outcomes. Waivers are listed below as debt. Separate test "
        "execution and implementation evidence remain necessary.\n\n"
        "This inventory assesses only the selected DRAFT corrective revision. It does "
        "not establish historical CLI conformance to the previous minor's "
        "req-mf-016 blanket project-root refusal. Human ratification and activation "
        "are UNSATISFIED. A prepared specification and collected bindings do not "
        "establish publication or ratification.\n\n"
        "Two qualified nonauthor human approvals (one with implementation experience "
        "and one with consumer/integrator experience), the process-issue label, "
        "and explicit human ratification/publication remain required. No human "
        "approval is recorded by this inventory. Only the public-comment requirement "
        "was waived by the [recorded decision]"
        "(https://github.com/microsoft/apm/issues/2818#issuecomment-5558647529). "
        "Automated reviews and passing spec-conformance checks do not ratify.\n\n"
        "## Conformance classes\n\n"
        "The four conformance classes (Producer, Consumer, Registry, "
        "Governance) are inventoried below, not certified by this report. The "
        "Registry binding includes the trust-anchor invariant "
        "test in `tests/spec_conformance/test_registry_reqs.py`, "
        "which hashes the committed Registry-archive fixture and "
        "asserts equality with the digest the paired lockfile "
        "advertises (sec.11.3.3, req-rg-001).\n\n"
        "## Repository case rules\n\n"
        "Repository-coordinate segments are case-insensitive for "
        "`github.com`, GitHub Enterprise Cloud hosts ending in `.ghe.com`, "
        "the literal GitHub Enterprise Server host selected by `GITHUB_HOST`, "
        "and registry-sourced dependencies (including registry prefixes). "
        "Local paths, marketplace identities, and every other host remain "
        "case-sensitive. Policy matching and repository identity use the same "
        "rule (req-rs-016 clause 3; req-pl-018).\n\n"
    )
    summary_section = "## Binding summary\n\n" + _md_class_summary(doc["summary_by_class"]) + "\n\n"
    user_scope = doc["consumer_user_scope"]
    scope_section = (
        "## Consumer user-scope disclosure\n\n"
        f"- Manifest: `{user_scope['manifest_location']}`\n"
        f"- Lockfile: `{user_scope['lockfile_location']}`\n"
        "- Target capability declaration: "
        f"`{user_scope['target_capability_declaration']}`\n\n"
    )
    rows = [
        "## Per-requirement bindings\n",
        "| Req ID | Keyword | Sec | Class | Status | Tests | Oracle |",
        "|--------|---------|----:|-------|--------|------:|--------|",
    ]
    for e in doc["requirements"]:
        rows.append(
            f"| [{e['id']}]({doc['spec_path']}#{e['id']}) "
            f"| {e['keyword']} | {e['section']} | {e['conformance_class']} "
            f"| {e['status']} | {e['test_count']} | {e.get('oracle', '-')} |"
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
    limitations = (
        "## Assessment limitations\n\n" + "\n\n".join(doc["assessment_limitations"]) + "\n\n"
    )
    return preamble + limitations + scope_section + summary_section + table + waivers_md


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
    try:
        write_outputs()
    except (ValueError, RuntimeError, OSError) as error:
        sys.stderr.write(f"[x] gen_statement: {error}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
