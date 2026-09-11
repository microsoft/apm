"""Assess JSON shape and source coverage, not complete prose correctness."""

import argparse
import json
import re
from pathlib import Path


def assess(output: Path, notes: Path, caution_prefix: str = "") -> tuple[int, str]:
    """Return the contract check protocol: pass 0, failed 1, incomplete 2."""
    try:
        source = notes.read_text(encoding="utf-8")
        candidate = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return 2, "Could not read the source notes or parse the candidate JSON."
    expected = re.findall(r"^- ([a-z][a-z0-9_-]*): ", source, flags=re.MULTILINE)
    if not expected or len(set(expected)) != len(expected):
        return 2, "Source notes need unique '- source_id: fact' entries."
    if not isinstance(candidate, list):
        return 1, "The handoff must be a JSON array."
    actual = []
    for item in candidate:
        if not isinstance(item, dict) or set(item) != {"source_id", "summary", "caution"}:
            return 1, "Every item must contain exactly source_id, summary and caution."
        if any(not isinstance(value, str) or not value.strip() for value in item.values()):
            return 1, "Every handoff value must be a nonempty string."
        if caution_prefix and not item["caution"].startswith(caution_prefix):
            return 1, "A caution does not follow the selected style criterion."
        actual.append(item["source_id"])
    if len(actual) != len(expected) or set(actual) != set(expected):
        return 1, "The handoff must cover each source ID exactly once."
    return 0, "JSON format is valid; every source note has one entry."


def main() -> int:
    """Run a noninteractive standard-library-only check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("notes", type=Path)
    parser.add_argument("--caution-prefix", default="")
    args = parser.parse_args()
    status, reason = assess(args.output, args.notes, args.caution_prefix)
    print(reason)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
