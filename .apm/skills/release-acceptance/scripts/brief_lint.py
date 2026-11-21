#!/usr/bin/env python3
"""Check that a worker brief uses neutral QA vocabulary.

Some model families refuse briefs worded as offensive security work
(observed in the 0.37 red team: HTTP 422 cyber-content refusals). The
same checks phrased as maintainers' QA verification run cleanly. This
lint flags the wording before a brief is sent.

stdout: JSON {"ok": bool, "hits": [{"word", "line", "suggest"}]}.
Exit 0 when clean, 1 when banned wording is present, 2 on usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BANNED = {
    "attack": "check",
    "attacker": "untrusted source",
    "exploit": "reproduce",
    "weaponize": "reproduce",
    "weaponise": "reproduce",
    "red team": "acceptance pass",
    "red-team": "acceptance",
    "hack": "probe",
    "adversary": "reviewer",
    "adversarial": "independent",
    "malicious": "untrusted",
    "payload": "input",
    "bypass": "gap",
    "pwn": "affect",
    "hunter": "worker",
}


def lint(text: str) -> list[dict]:
    hits: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        for word, suggest in BANNED.items():
            if re.search(rf"\b{re.escape(word)}\w*", lowered):
                hits.append({"word": word, "line": lineno, "suggest": suggest})
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint a worker brief for neutral wording.")
    parser.add_argument("brief", help="path to the brief text")
    args = parser.parse_args(argv)
    path = Path(args.brief)
    if not path.is_file():
        print(f"brief_lint: {path} not found", file=sys.stderr)
        return 2
    hits = lint(path.read_text(encoding="utf-8"))
    print(json.dumps({"ok": not hits, "hits": hits}, indent=2))
    return 0 if not hits else 1


if __name__ == "__main__":
    sys.exit(main())
