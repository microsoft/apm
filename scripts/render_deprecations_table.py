#!/usr/bin/env python3
"""Render the CLI reference deprecations tables from ``core/deprecations.py``.

The catalog is the single owner of deprecation metadata; the docs table is
generated from it so the two never drift. ``--check`` (the default) exits 1
when the page is stale; ``--write`` rewrites the generated block in place.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from apm_cli.core.deprecations import (
    _CATALOG,
    NOT_SCHEDULED,
    SIGNAL_COLLECTED,
    SIGNAL_PYTHON,
    UNKNOWN_SINCE,
    WARNING_FREE_DEPRECATIONS,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PAGE = REPO_ROOT / "docs" / "src" / "content" / "docs" / "reference" / "index.md"
BEGIN = "<!-- BEGIN GENERATED: deprecations (scripts/render_deprecations_table.py) -->"
END = "<!-- END GENERATED: deprecations -->"

_SIGNALS = {
    SIGNAL_PYTHON: "Python `DeprecationWarning` (hidden by default)",
    SIGNAL_COLLECTED: "Collected, not printed",
}


def _version(value: str | None, missing: str) -> str:
    return f"v{value}" if value else missing


def render_tables() -> str:
    """Return the generated Markdown block, markers included."""
    lines = [
        BEGIN,
        "| Deprecated | Use instead | Deprecated since | Removal | How APM tells you |",
        "|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {entry.surface} | {entry.replacement} | {_version(entry.since, UNKNOWN_SINCE)} "
        f"| {_version(entry.removal, NOT_SCHEDULED)} | {_SIGNALS.get(entry.signal, 'Warning')} |"
        for entry in _CATALOG
    )
    lines.extend(
        [
            "",
            "Deprecated without a warning today:",
            "",
            "| Deprecated | Use instead | Kind |",
            "|---|---|---|",
        ]
    )
    lines.extend(
        f"| {item.surface} | {item.replacement} | {item.kind} |"
        for item in WARNING_FREE_DEPRECATIONS
    )
    lines.append(END)
    return "\n".join(lines)


def rendered_page(page: str) -> str:
    """Return *page* with its generated block replaced by the current tables."""
    start = page.index(BEGIN)
    stop = page.index(END, start) + len(END)
    return page[:start] + render_tables() + page[stop:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the generated block")
    args = parser.parse_args(argv)
    page = DOC_PAGE.read_text(encoding="utf-8")
    expected = rendered_page(page)
    if page == expected:
        return 0
    if args.write:
        DOC_PAGE.write_text(expected, encoding="utf-8")
        return 0
    print(
        f"[x] {DOC_PAGE.relative_to(REPO_ROOT).as_posix()} deprecations table is stale. "
        "Run 'uv run python scripts/render_deprecations_table.py --write'.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
