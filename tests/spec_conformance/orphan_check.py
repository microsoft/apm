"""4-way bind check between spec body anchors, manifest, Appendix C,
and pytest req markers.

CANONICAL SOURCE: the HTML `<a id="req-XXX"></a>` anchors in the spec
body. The manifest, Appendix C row table, and pytest markers are all
projections that MUST match the canonical set exactly. Drift in any
direction fails this gate.

Invoke as:
    uv run python -m tests.spec_conformance.orphan_check

Exit codes:
    0  all four sets agree
    1  divergence (diff printed to stderr)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

from tests.spec_conformance._manifest import (
    COVERAGE_PATH,
    MANIFEST_PATH,
    REPO_ROOT,
    SPEC_PATH,
    load_requirements,
)

ANCHOR_RE = re.compile(r'<a id="(req-[a-z]{2,3}-[0-9]{3})"></a>')
APPC_RE = re.compile(
    r"^\|\s*\[(req-[a-z]{2,3}-[0-9]{3})\]\(#\1\)\s*\|"
    r"\s*(MUST NOT|SHOULD NOT|MUST|SHOULD|MAY)\s*\|"
    r"\s*([0-9.]+)\s*\|"
    r"\s*(producer|consumer|registry|governance)\s*\|"
)


def set_anchors() -> set[str]:
    text = SPEC_PATH.read_text(encoding="utf-8")
    return set(ANCHOR_RE.findall(text))


def set_manifest() -> set[str]:
    return {r.id for r in load_requirements()}


def appendix_c_rows() -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    in_app = False
    for raw in SPEC_PATH.read_text(encoding="utf-8").splitlines():
        if raw.startswith("## Appendix C"):
            in_app = True
            continue
        if in_app and raw.startswith("## Appendix D"):
            break
        if not in_app:
            continue
        m = APPC_RE.match(raw)
        if m:
            rows.append(m.groups())
    return rows


def set_appc() -> set[str]:
    return {row[0] for row in appendix_c_rows()}


def set_markers() -> set[str]:
    """Run pytest in collect-only mode and harvest the coverage map.

    We invoke pytest in a sub-process to honour the spec_conformance
    conftest's marker-validation step. The coverage map is written by
    pytest_collection_modifyitems.
    """
    if COVERAGE_PATH.exists():
        COVERAGE_PATH.unlink()
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/spec_conformance",
        "--collect-only",
        "-q",
        "-p",
        "no:randomly",
        "--no-header",
    ]
    res = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if res.returncode != 0 and not COVERAGE_PATH.exists():
        sys.stderr.write(
            "orphan_check: pytest --collect-only failed before producing "
            "coverage map. pytest stderr below:\n"
        )
        sys.stderr.write(res.stderr)
        sys.exit(2)
    if not COVERAGE_PATH.exists():
        return set()
    with COVERAGE_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    return set(data.keys())


def diff_report(label_a: str, set_a: set[str], label_b: str, set_b: set[str]) -> list[str]:
    only_a = sorted(set_a - set_b)
    only_b = sorted(set_b - set_a)
    out: list[str] = []
    if only_a:
        out.append(f"  in {label_a} but not in {label_b}:")
        out.extend(f"    - {x}" for x in only_a)
    if only_b:
        out.append(f"  in {label_b} but not in {label_a}:")
        out.extend(f"    - {x}" for x in only_b)
    return out


def check_appendix_c_consistency() -> list[str]:
    """Appendix C rows MUST agree field-for-field with the manifest."""
    by_id = {r.id: r for r in load_requirements()}
    problems: list[str] = []
    for req_id, keyword, section, klass in appendix_c_rows():
        entry = by_id.get(req_id)
        if entry is None:
            continue
        if entry.keyword != keyword:
            problems.append(f"  {req_id}: keyword Appendix C={keyword} != manifest={entry.keyword}")
        if entry.section != section:
            problems.append(f"  {req_id}: section Appendix C={section} != manifest={entry.section}")
        if entry.conformance_class != klass:
            problems.append(
                f"  {req_id}: class Appendix C={klass} != manifest={entry.conformance_class}"
            )
    return problems


def main() -> int:
    anchors = set_anchors()
    manifest = set_manifest()
    appc = set_appc()
    markers = set_markers()
    failures: list[str] = []
    if anchors != manifest:
        failures.append("[x] anchors != manifest")
        failures.extend(diff_report("anchors", anchors, "manifest", manifest))
    if anchors != appc:
        failures.append("[x] anchors != Appendix C")
        failures.extend(diff_report("anchors", anchors, "Appendix C", appc))
    if anchors != markers:
        failures.append("[x] anchors != pytest req markers")
        failures.extend(diff_report("anchors", anchors, "markers", markers))
    field_problems = check_appendix_c_consistency()
    if field_problems:
        failures.append("[x] Appendix C fields disagree with manifest:")
        failures.extend(field_problems)
    if failures:
        sys.stderr.write("\n".join(failures) + "\n")
        sys.stderr.write(
            "\nFix the canonical source first (HTML anchors in "
            f"{SPEC_PATH.relative_to(REPO_ROOT)}), then realign the "
            f"manifest ({MANIFEST_PATH.relative_to(REPO_ROOT)}), "
            "Appendix C, and add/extend a @pytest.mark.req(...) test.\n"
        )
        return 1
    print(
        f"[+] orphan_check OK: {len(anchors)} requirements aligned across "
        "anchors / manifest / Appendix C / pytest markers"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
