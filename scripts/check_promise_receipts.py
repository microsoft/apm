"""Promise-obligation receipts: shrink-only baseline, generator, negative control (#4122).

``tests/quality/promise_obligations.json`` (steward-owned) splits the obligation
universe into ``required`` rows, each witnessed by a lifecycle test that must
earn an oracle receipt for it, and a ``baseline`` of residual rows no receipt
covers yet. ``tests/quality/test_promise_obligations.py`` is the gate; this
script adds what the gate cannot see from one checkout:

  --base-ref REF     fail when the baseline gained a row absent at REF
                     (the baseline may only shrink)
  --write-baseline   regenerate the baseline from merged receipts: every
                     baseline row a receipt now covers moves to required,
                     witnessed by the first covering test
  --drop-receipt     negative control: remove the one merged receipt that alone
                     covers a required row, so the gate must fail and name it

Run with ``python -m scripts.check_promise_receipts`` from the repository root.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.ratchet_baseline import BaselineError, load_baseline_at_ref
from tests.lifecycle import receipts
from tests.lifecycle.obligations import OBLIGATIONS_PATH, Catalog, load_catalog

LABEL = "promise-obligation"
DEFAULT_RECEIPTS = Path("build/lifecycle-receipts")


def baseline_growth(root: Path, path: Path, ref: str) -> list[str] | None:
    """Baseline rows absent at *ref*, or None when *ref* has no obligation file."""
    base = load_baseline_at_ref(root, path, ref, label=LABEL)
    if base is None:
        return None
    base_rows = base.get("baseline")
    if not isinstance(base_rows, dict):
        raise BaselineError(f"invalid {LABEL} baseline at {ref}: 'baseline' must be an object")
    current = json.loads(path.read_text(encoding="ascii"))["baseline"]
    return sorted(set(current) - set(base_rows))


def regenerate(catalog: Catalog, earned: list[receipts.Receipt]) -> dict[str, object]:
    """The obligation document with every receipted baseline row promoted to required."""
    data = json.loads(json.dumps(catalog.raw))
    promoted: dict[str, str] = {}
    for key in sorted(catalog.baseline):
        obligation = catalog.obligations[key]
        nodes = sorted({receipt.node for receipt in earned if obligation.covered_by(receipt)})
        if nodes:
            promoted[key] = nodes[0]
    data["required"] = dict(sorted({**data["required"], **promoted}.items()))
    data["baseline"] = {
        key: lane for key, lane in sorted(data["baseline"].items()) if key not in promoted
    }
    return data


def _covering(catalog: Catalog, key: str, merged: list[receipts.Receipt]) -> list[int]:
    witness, obligation = catalog.required[key], catalog.obligations[key]
    return [
        index
        for index, receipt in enumerate(merged)
        if receipt.node == witness and obligation.covered_by(receipt)
    ]


def drop_receipts(catalog: Catalog, directory: Path) -> tuple[str, str, int]:
    """Remove the receipts that alone cover one required row of the merged session.

    Prefers the first required row a single receipt covers, so the control drops
    exactly one receipt; otherwise drops every receipt covering the first row.
    """
    paths = sorted(directory.glob("session-*.jsonl"))
    lines = [line for path in paths for line in path.read_text(encoding="ascii").splitlines()]
    lines = [line for line in lines if line.strip()]
    merged = [receipts.Receipt.from_json(line) for line in lines]
    ordered = sorted(catalog.required)
    covering = {key: _covering(catalog, key, merged) for key in ordered}
    key = next((key for key in ordered if len(covering[key]) == 1), ordered[0])
    dropped = set(covering[key])
    for path in paths:
        path.unlink()
    kept = [line for index, line in enumerate(lines) if index not in dropped]
    (directory / "session-control.jsonl").write_text(
        "".join(f"{line}\n" for line in kept), encoding="ascii"
    )
    return key, catalog.required[key], len(dropped)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--receipts-dir", type=Path, default=DEFAULT_RECEIPTS)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--base-ref", help="Git ref whose baseline the working one may not exceed.")
    action.add_argument("--write-baseline", action="store_true")
    action.add_argument("--drop-receipt", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = args.root.resolve()
    path = root / OBLIGATIONS_PATH.relative_to(OBLIGATIONS_PATH.parents[2])
    if args.base_ref is not None:
        added = baseline_growth(root, path, args.base_ref)
        if added is None:
            print(f"[i] no {LABEL} baseline at {args.base_ref}; skipping the shrink-only check")
            return 0
        if added:
            print(
                f"[x] {LABEL} baseline grew against {args.base_ref}; earn oracle receipts "
                "for these rows instead of baselining them:",
                file=sys.stderr,
            )
            print("\n".join(f"  {key}" for key in added), file=sys.stderr)
            return 1
        print(f"[+] {LABEL} baseline did not grow against {args.base_ref}")
        return 0

    catalog = load_catalog(path)
    receipts_dir = root / args.receipts_dir
    if args.drop_receipt:
        key, witness, removed = drop_receipts(catalog, receipts_dir)
        if not removed:
            print(f"[x] {witness} earned no receipt for {key}; nothing to drop", file=sys.stderr)
            return 2
        print(f"[*] dropped {removed} receipt(s) of {witness} covering {key}")
        return 0

    data = regenerate(catalog, receipts.load(receipts_dir))
    promoted = sorted(set(catalog.baseline) - set(data["baseline"]))
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="ascii")
    print(f"[+] promoted {len(promoted)} row(s); {len(data['baseline'])} residual row(s)")
    for key in promoted:
        print(f"  {key} -> {data['required'][key]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
