#!/usr/bin/env python3
"""Keep the taxonomy allowance lists shrink-only against a base revision.

``tests/quality/test_taxonomy_baseline.json`` grandfathers unclassified,
misplaced and promise-less modules. Adding an entry would grandfather new
debt, so every entry that is not in the base revision's copy fails, except
a validated move: the added module exists and replaces a base entry with the
same file name whose module no longer exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ratchet_baseline import BaselineError, load_baseline_at_ref

LISTS = ("misplaced_modules", "promise_exempt_modules", "unclassified_modules")
BASELINE = Path("tests") / "quality" / "test_taxonomy_baseline.json"


def growth(
    root: Path,
    current: dict[str, object],
    base: dict[str, object],
) -> list[str]:
    """Return one failure per added entry that is not a validated move."""
    failures: list[str] = []
    for key in LISTS:
        now = set(current.get(key, []))  # type: ignore[arg-type]
        before = set(base.get(key, []))  # type: ignore[arg-type]
        gone: dict[str, list[str]] = {}
        for entry in sorted(before - now):
            if not (root / entry).exists():
                gone.setdefault(Path(entry).name, []).append(entry)
        for entry in sorted(now - before):
            sources = gone.get(Path(entry).name)
            if sources and (root / entry).is_file():
                sources.pop(0)
                continue
            failures.append(
                f"{entry}: added to {key} in {BASELINE.as_posix()}. Fix: classify, "
                "place or promise-link the module instead; the allowance lists only "
                "shrink (a move keeps its entry only when the old path is gone)."
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base-ref", required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    path = root / BASELINE
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        base = load_baseline_at_ref(root, path, args.base_ref, label="taxonomy")
    except (OSError, ValueError, BaselineError) as error:
        print(f"[x] taxonomy allowance check failed: {error}", file=sys.stderr)
        return 2
    if base is None:
        print("[i] taxonomy baseline absent at base ref; nothing to compare")
        return 0
    failures = growth(root, current, base)
    for failure in failures:
        print(f"[x] {failure}", file=sys.stderr)
    if failures:
        return 1
    print("[+] taxonomy allowances did not grow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
