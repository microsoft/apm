"""Rule H4: a pull request that closes an issue a strict known cell cites flips that cell (#4565).

When a PR closes ("Fixes #N") an issue that a strict known cell still cites,
the next snapshot refresh records the issue as closed and every later PR goes
red on ``test_known_cells_cite_open_issues.py``. This gate fails the closing PR
itself instead. CI pipes the PR body in:

    python -m tests.quality.known_cell_closing [BODY_FILE]   # stdin without a file
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from tests.quality.known_cell_issues import REPO, known_cells

# GitHub's closing keywords, then "#N" or "<owner>/<repo>#N"; one issue per keyword.
_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<n>\d+)\b",
    re.IGNORECASE,
)
# Markdown GitHub does not render as text: no issue reference is linked there.
_UNRENDERED = re.compile(r"<!--.*?-->|```.*?```|`[^`\n]*`", re.DOTALL)


def closed_issues(body: str) -> set[int]:
    """Issue numbers of ``REPO`` the PR body closes through a closing keyword."""
    text = _UNRENDERED.sub(" ", body)
    return {
        int(match["n"])
        for match in _CLOSING.finditer(text)
        if match["repo"] is None or match["repo"].lower() == REPO
    }


def cells_closed_by(closed: set[int], cells: dict[str, str]) -> dict[str, str]:
    """``{cell: "#<issue>"}`` for every strict known cell citing an issue in *closed*."""
    return {
        label: tracking
        for label, tracking in cells.items()
        if re.fullmatch(r"#\d+", tracking) and int(tracking[1:]) in closed
    }


def main(argv: list[str]) -> int:
    body = Path(argv[0]).read_text(encoding="utf-8") if argv else sys.stdin.read()
    closed = closed_issues(body)
    offenders = cells_closed_by(closed, known_cells()) if closed else {}
    if not offenders:
        print(f"[+] H4: no strict known cell cites an issue this PR closes {sorted(closed)}")
        return 0
    print("[x] H4: this PR closes issues that strict known cells still cite:")
    for label, tracking in offenders.items():
        print(f"    {label} -> {tracking}")
    print(
        "    Flip each cell to required (or re-key it to an open issue) in this PR, then run "
        "'python -m tests.quality.known_cell_issues --write'."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
