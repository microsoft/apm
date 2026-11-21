"""Tracking issues of every strict known cell and their recorded GitHub state (#4565).

A strict known cell stays a known failure only while its tracking issue is
open. Reading issue state needs the network, so the gate
(``test_known_cells_cite_open_issues.py``) reads an offline snapshot,
``known_cell_issue_states.json``. Refresh it with gh after a cell or an issue
changes:

    python -m tests.quality.known_cell_issues --write
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).parents[2]
SNAPSHOT_PATH = Path(__file__).with_name("known_cell_issue_states.json")
GENERATED_PATH = Path(__file__).with_name("generated_obligations.json")
REPO = "microsoft/apm"


def _machine_cells() -> Iterable[tuple[str, str]]:
    from tests.lifecycle import machines

    for info in pkgutil.iter_modules(machines.__path__):
        if not info.name.startswith(("test_", "_")):
            continue
        module = importlib.import_module(f"{machines.__name__}.{info.name}")
        for cell in getattr(module, "KNOWN_CELLS", ()):
            yield f"machine:{cell.cell_id}", cell.tracking


def known_cells() -> dict[str, str]:
    """``{cell label: "#<issue>"}`` for every strict known cell of the lifecycle family."""
    from tests.lifecycle.generator.rules import RULE_CELL_KNOWN
    from tests.lifecycle.journeys.test_journeys import _WINDOWS_KNOWN, journey_files, load_journey
    from tests.lifecycle.world.test_world import _KNOWN as WORLD_KNOWN

    cells: dict[str, str] = {}
    generated = json.loads(GENERATED_PATH.read_text(encoding="ascii"))["known_failing"]
    cells.update({f"generated:{key}": tracking for key, tracking in generated.items()})
    for path in journey_files():
        journey = load_journey(path)
        for cell in journey.get("known_cells") or ():
            cells[f"journey:{journey['id']}:{cell['id']}"] = str(cell["tracking"])
    cells.update(_machine_cells())
    cells.update({f"rule:{key}": tracking for key, (tracking, _) in RULE_CELL_KNOWN.items()})
    cells.update({f"world:{p}|{s}": known.tracking for (p, s), known in WORLD_KNOWN.items()})
    cells.update({f"windows:{jid}": known.tracking for jid, known in _WINDOWS_KNOWN.items()})
    return dict(sorted(cells.items()))


def cited_issues(cells: dict[str, str] | None = None) -> list[int]:
    cells = known_cells() if cells is None else cells
    return sorted({int(tracking.lstrip("#")) for tracking in cells.values()})


def load_snapshot() -> dict[str, str]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="ascii"))["issues"]


def _fetch_state(number: int) -> str:
    out = subprocess.run(
        ["gh", "issue", "view", str(number), "--repo", REPO, "--json", "state", "-q", ".state"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def main(argv: list[str]) -> int:
    issues = {str(n): _fetch_state(n) for n in cited_issues()}
    if "--write" not in argv:
        stale = issues != load_snapshot()
        print("[x] snapshot is stale; run with --write" if stale else "[+] snapshot is current")
        return int(stale)
    data = {
        "description": (
            "GitHub state of every issue a strict known cell cites; regenerate with "
            "'python -m tests.quality.known_cell_issues --write'."
        ),
        "issues": issues,
    }
    SNAPSHOT_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="ascii")
    print(f"[+] {len(issues)} issues recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
