"""Replay rows absorbed from tests/integration/*_lifecycle*.py (#4114).

Each ``replay/rows/absorbed_*.yaml`` row names the integration test cell it
absorbs in its header comment. The row replays through the driver and keeps
I1-I9 on every step (``tests/lifecycle/rows.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.lifecycle.rows import RowReplay, load_row

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I3",
        "P-INV-I4",
        "P-INV-I5",
        "P-INV-I7",
        "P-INV-I8",
        "P-LAW-outcome.status_matches_state",
    ),
]

ROWS = sorted((Path(__file__).parent / "replay" / "rows").glob("absorbed_*.yaml"))


@pytest.mark.parametrize("row_path", ROWS, ids=[path.stem for path in ROWS])
def test_absorbed_row_keeps_every_invariant(lifecycle_world, row_path: Path) -> None:
    RowReplay(lifecycle_world, load_row(row_path)).replay()
