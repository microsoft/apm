"""Rule H4: a pull request that closes an issue a strict known cell cites flips that cell (#4565).

When a PR closes ("Fixes #N") an issue that a strict known cell still cites,
the next snapshot refresh records the issue as closed and every later PR goes
red on ``test_known_cells_cite_open_issues.py``. This gate fails the closing PR
itself instead. CI passes the PR body and the PR's commit messages (a merge
commit lands them on main, where their closing keywords close issues too):

    python -m tests.quality.known_cell_closing BODY_FILE --commits FILE --commit-count N
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from markdown_it import MarkdownIt

from tests.quality.known_cell_issues import REPO, known_cells

# GitHub's closing keywords, then "#N" or "<owner>/<repo>#N"; one issue per keyword.
_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<n>\d+)\b",
    re.IGNORECASE,
)
# Inline children -> text. Emphasis and link markup are transparent; a dropped
# code span or inline HTML leaves a barrier so it cannot splice a keyword.
_INLINE = {"softbreak": "\n", "hardbreak": "\n", "code_inline": "\x00", "html_inline": "\x00"}


def _prose(markdown: str) -> str:
    """The rendered text of *markdown*: code spans, code blocks and HTML link no issue."""
    return "\n".join(
        "".join(
            child.content if child.type == "text" else _INLINE.get(child.type, "")
            for child in token.children or ()
        )
        for token in MarkdownIt("commonmark").parse(markdown)
        if token.type == "inline"
    )


def closed_issues(body: str) -> set[int]:
    """Issue numbers of ``REPO`` that *body* closes through a closing keyword."""
    text = _prose(body)
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


def report(texts: list[str]) -> int:
    """Print the H4 verdict over *texts* (PR body, commit messages); 1 on a violation."""
    closed = set().union(*map(closed_issues, texts))
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


def commit_messages(jsonl: str, expected: int) -> list[str]:
    """One JSON string per line (``gh api --jq '.[].commit.message | @json'``)."""
    messages = [json.loads(line) for line in jsonl.splitlines() if line.strip()]
    if len(messages) != expected:
        raise SystemExit(
            f"[x] H4: read {len(messages)} of the PR's {expected} commit messages "
            "(the API lists at most 250); cannot check them, failing closed"
        )
    return messages


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.quality.known_cell_closing")
    parser.add_argument("texts", nargs="*", help="Markdown files (PR body); stdin if none")
    parser.add_argument("--commits", help="commit messages, one JSON string per line")
    parser.add_argument("--commit-count", type=int, help="the PR's commit count")
    args = parser.parse_args(argv)
    if (args.commits is None) != (args.commit_count is None):
        parser.error("--commits and --commit-count go together")
    texts = [Path(arg).read_text(encoding="utf-8") for arg in args.texts]
    if not args.texts and args.commits is None:
        texts = [sys.stdin.read()]
    if args.commits is not None:
        jsonl = Path(args.commits).read_text(encoding="utf-8")
        texts += commit_messages(jsonl, args.commit_count)
    return report(texts)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
