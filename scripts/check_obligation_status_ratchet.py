#!/usr/bin/env python3
"""Fail when a required lifecycle obligation loses its witness without a named rule.

An obligation id that is ``required`` at the base ref (in
``tests/quality/lifecycle_obligations.json`` or ``generated_obligations.json``)
must still be ``required`` or ``known-failing`` at the head. Deleting a
witness and regenerating would otherwise demote it to ``baseline`` (or drop it)
with no gate (#4586). The only way out is a rule the same change adds and
names:

- a generator removal or mapping rule: a new ``removed`` or ``mapped`` entry
  in ``lifecycle_obligations.json``;
- a reviewed exclusion: a new ``excluded`` entry in ``security_obligations.json``;
- a named removal: a new ``removals`` entry in ``obligation_removals.json``
  (``"<id>": "<rule or ruling, with its issue>"``), for ids no generator
  rule can express (journey oracles, for example).

Usage: ``python scripts/check_obligation_status_ratchet.py --base-ref <sha>``
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from ratchet_baseline import BaselineError, load_baseline_at_ref

ROOT = Path(__file__).resolve().parents[1]
QUALITY = Path("tests/quality")
LIFECYCLE = QUALITY / "lifecycle_obligations.json"
GENERATED = QUALITY / "generated_obligations.json"
SECURITY = QUALITY / "security_obligations.json"
REMOVALS = QUALITY / "obligation_removals.json"


def _at_head(root: Path, path: Path) -> dict:
    file = root / path
    return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}


def _at_base(root: Path, path: Path, ref: str) -> dict:
    return load_baseline_at_ref(root, root / path, ref, label=path.name) or {}


def required_ids(lifecycle: Mapping, generated: Mapping) -> set[str]:
    """Ids a run must credit: ``required`` in either obligation file."""
    rows = lifecycle.get("obligations", {})
    return {key for key, row in rows.items() if row.get("status") == "required"} | set(
        generated.get("required", {})
    )


def kept_ids(lifecycle: Mapping, generated: Mapping) -> set[str]:
    """Ids still witnessed or tracked: ``required`` or ``known-failing``."""
    rows = lifecycle.get("obligations", {})
    kept = {key for key, row in rows.items() if row.get("status") in ("required", "known-failing")}
    return kept | set(generated.get("required", {})) | set(generated.get("known_failing", {}))


def ruled_ids(base: Mapping[str, Mapping], head: Mapping[str, Mapping]) -> set[str]:
    """Ids a rule names at the head but not at the base."""

    def named(files: Mapping[str, Mapping]) -> set[str]:
        lifecycle, security = files["lifecycle"], files["security"]
        return (
            set(lifecycle.get("removed", {}))
            | set(lifecycle.get("mapped", {}))
            | set(security.get("excluded", {}))
            | {
                key
                for key, rule in files["removals"].get("removals", {}).items()
                if isinstance(rule, str) and rule.strip()
            }
        )

    return named(head) - named(base)


def _load(root: Path, ref: str | None) -> dict[str, dict]:
    paths = {
        "lifecycle": LIFECYCLE,
        "generated": GENERATED,
        "security": SECURITY,
        "removals": REMOVALS,
    }
    if ref is None:
        return {name: _at_head(root, path) for name, path in paths.items()}
    return {name: _at_base(root, path, ref) for name, path in paths.items()}


def demoted_between(base: Mapping[str, Mapping], head: Mapping[str, Mapping]) -> list[str]:
    """Ids required in *base* that *head* neither keeps nor rules out, sorted."""
    lost = required_ids(base["lifecycle"], base["generated"]) - kept_ids(
        head["lifecycle"], head["generated"]
    )
    return sorted(lost - ruled_ids(base, head))


def demoted(root: Path, ref: str) -> list[str]:
    """Ids required at *ref* that the working tree neither keeps nor rules out."""
    return demoted_between(_load(root, ref), _load(root, None))


def _report(ids: Iterable[str], ref: str) -> None:
    ids = list(ids)
    print(f"[x] {len(ids)} obligation(s) required at {ref} are baseline or absent now:")
    for key in ids:
        print(f"    - {key}")
    print(
        "[i] Restore the witness, or name the rule that removes the obligation: a generator "
        f"removed/mapped rule, a security exclusion, or an entry in {REMOVALS.as_posix()}."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-ref", required=True, help="merge base to compare with")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        lost = demoted(args.root, args.base_ref)
    except BaselineError as error:
        print(f"[x] {error}")
        return 2
    if lost:
        _report(lost, args.base_ref)
        return 1
    print(f"[+] obligation status ratchet clean against {args.base_ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
