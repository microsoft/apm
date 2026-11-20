#!/usr/bin/env python3
"""Check the test-coverage-expert activation rule against fixture cases.

This is a SPECIFICATION TEST, not the production roster. The orchestrator
LLM applies the rule in SKILL.md ("Test Coverage Expert") and
assets/panel-mode.md; this script encodes the same rule so a maintainer
can confirm the fixture cases agree with it offline.

Rule: spawn when the diff touches a src/**/*.py path, or when it has a
test cut (assets/panel-mode.md "Test cut"): a tests/ path that is D or
R (old path) in `git diff --name-status`, or a tests/ patch that removes
a `def test_` line, removes more assert / pytest.raises lines than it
adds, or adds a `--no-policy` literal.

Usage:
    python3 roster_eval.py
    python3 roster_eval.py roster-cases.json

Exit code 0 when every case matches, 1 otherwise.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEFAULT_FIXTURE = Path(__file__).parent / "roster-cases.json"


TEST_DEF = re.compile(r"^\s*(async\s+)?def test_")
ASSERTION = re.compile(r"\bassert\b|pytest\.raises")


def _patch_has_cut(lines: list[str]) -> bool:
    removed = [ln[1:] for ln in lines if ln.startswith("-") and not ln.startswith("---")]
    added = [ln[1:] for ln in lines if ln.startswith("+") and not ln.startswith("+++")]
    if any(TEST_DEF.match(ln) for ln in removed):
        return True
    if sum(bool(ASSERTION.search(ln)) for ln in removed) > sum(
        bool(ASSERTION.search(ln)) for ln in added
    ):
        return True
    return any("--no-policy" in ln for ln in added)


def coverage_panelist_active(name_status: list[str], patches: dict[str, list[str]]) -> bool:
    for line in name_status:
        status, *paths = line.split("\t")
        if any(p.startswith("src/") and p.endswith(".py") for p in paths):
            return True
        if status[:1] in ("D", "R") and paths and paths[0].startswith("tests/"):
            return True
    return any(
        path.startswith("tests/") and _patch_has_cut(lines) for path, lines in patches.items()
    )


def main(argv: list[str]) -> int:
    fixture = Path(argv[1]) if len(argv) > 1 else DEFAULT_FIXTURE
    text = fixture.read_text(encoding="utf-8")
    if not text.isascii():
        print(f"[x] {fixture.name}: non-ASCII content")
        return 1
    failures = 0
    for case in json.loads(text)["cases"]:
        actual = coverage_panelist_active(case["name_status"], case.get("patches", {}))
        ok = actual == case["expect_active"]
        failures += not ok
        mark = "[OK]" if ok else "[FAIL]"
        print(f"{mark} {case['name']}: expect={case['expect_active']} actual={actual}")
    print(f"{'[+]' if not failures else '[x]'} {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
