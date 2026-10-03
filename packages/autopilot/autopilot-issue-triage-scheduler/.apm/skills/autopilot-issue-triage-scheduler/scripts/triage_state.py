#!/usr/bin/env python3
"""Plan advisory metadata from JSON on stdin; never call GitHub or authorize work."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def plan_batch(data: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Select from accumulated oldest-first pages and plan only permitted labels.

    Input issues have number, author, labels (names), eligible (the caller's
    body/state filter), and optional proposed_labels. Explicit requests ignore
    completed-advice markers, but never eligibility or label-availability checks.
    """
    mode = data["mode"]
    if mode not in {"sweep", "label-event", "dispatch"}:
        raise ValueError("mode must be sweep, label-event, or dispatch")
    processing = contract["processing"]
    marker = processing["active_write_reviewed"]
    repository_labels = set(data["repository_labels"])
    if marker not in repository_labels:
        raise ValueError(f"Active processing label {marker} is unavailable; no labels created")
    if mode != "sweep" and len(data["issues"]) != 1:
        raise ValueError("Explicit requests require exactly one issue")

    completed = set(processing["read_reviewed"])
    allowed = set(contract["classification_labels"]) & repository_labels
    aliases = contract["legacy_read_aliases"]
    selected = []
    per_author: dict[str, int] = {}
    seen: set[int] = set()
    for issue in data["issues"]:
        number = issue["number"]
        if type(number) is not int or number <= 0:
            raise ValueError("Issue number must be a positive integer")
        if number in seen:
            continue
        seen.add(number)
        existing = set(issue["labels"])
        if issue["eligible"] is not True or (mode == "sweep" and existing & completed):
            continue
        author = issue["author"]
        if mode == "sweep" and per_author.get(author, 0) >= 2:
            continue
        normalized = existing | {aliases.get(label, label) for label in existing}
        occupied = {label.split("/", 1)[0] for label in normalized}
        proposed = set(issue.get("proposed_labels", [])) & allowed
        additions = {label for label in proposed if label.split("/", 1)[0] not in occupied}
        for dimension in ("type", "theme"):
            values = sorted(label for label in additions if label.startswith(f"{dimension}/"))
            if len(values) > 1:
                raise ValueError(f"Conflicting proposed {dimension} labels")
        if len(additions) > 6:
            raise ValueError("At most six classification labels may be proposed")
        additions.add(marker)
        selected.append(
            {
                "number": number,
                "add_labels": sorted(additions - existing),
                "remove_labels": sorted(existing & set(processing["removable"])),
            }
        )
        per_author[author] = per_author.get(author, 0) + 1
        if len(selected) == 10:
            break
    return {
        "selected": selected,
        "batch_full": len(selected) == 10,
        "implementation_authorized": False,
    }


def main() -> int:
    """Read a normalized batch and emit a JSON plan or an actionable error."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        contract = json.loads(
            (Path(__file__).resolve().parent.parent / "assets/label-contract.json").read_text(
                encoding="ascii"
            )
        )
        result = plan_batch(json.load(sys.stdin), contract)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"[x] Triage metadata planning failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
