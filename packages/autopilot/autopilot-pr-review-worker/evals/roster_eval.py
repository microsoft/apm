#!/usr/bin/env python3
"""Check the test-coverage-expert activation rule against fixture cases.

This is a SPECIFICATION TEST, not the production roster. The orchestrator
LLM applies the rule in SKILL.md ("Test Coverage Expert") and
assets/panel-mode.md; this script encodes the same rule so a maintainer
can confirm the fixture cases agree with it offline.

Rule: spawn when the diff touches a src/**/*.py path, or when a
`git diff --name-status` entry is D (deleted) or R (moved or renamed)
with an old path under tests/.

Usage:
    python3 roster_eval.py
    python3 roster_eval.py roster-cases.json

Exit code 0 when every case matches, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_FIXTURE = Path(__file__).parent / "roster-cases.json"


def coverage_panelist_active(name_status: list[str]) -> bool:
    for line in name_status:
        status, *paths = line.split("\t")
        if any(p.startswith("src/") and p.endswith(".py") for p in paths):
            return True
        if status[:1] in ("D", "R") and paths and paths[0].startswith("tests/"):
            return True
    return False


def main(argv: list[str]) -> int:
    fixture = Path(argv[1]) if len(argv) > 1 else DEFAULT_FIXTURE
    text = fixture.read_text(encoding="utf-8")
    if not text.isascii():
        print(f"[x] {fixture.name}: non-ASCII content")
        return 1
    failures = 0
    for case in json.loads(text)["cases"]:
        actual = coverage_panelist_active(case["name_status"])
        ok = actual == case["expect_active"]
        failures += not ok
        mark = "[OK]" if ok else "[FAIL]"
        print(f"{mark} {case['name']}: expect={case['expect_active']} actual={actual}")
    print(f"{'[+]' if not failures else '[x]'} {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
