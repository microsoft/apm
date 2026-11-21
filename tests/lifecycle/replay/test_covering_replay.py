"""Generated covering replay rows through the real CLI (#4099).

``python -m tests.lifecycle.generator`` writes one row per covering set in
``tests/lifecycle/replay/rows/generated_*.yaml``; each row runs here as one
pytest node, so the lifecycle smoke shards split them. Every step keeps the
cross-cutting laws I1-I9 (``RowReplay``); a step's ``cells`` are credited
through ``oracles.check_obligation`` only after the step passed, and the
receipts witness the ``required`` ids of ``tests/quality/lifecycle_obligations.json``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.lifecycle.generator.covering import WORLD_KINDS
from tests.lifecycle.generator.replay import CoveringReplay, load_covering_row

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        *(f"P-INV-I{n}" for n in range(1, 10)),
        "P-OWN-executable-item-admission",
        "P-OWN-frozen-install-mutation-eligibility",
        "P-LAW-transaction.failed_command_preserves_state",
    ),
]

ROWS_DIR = Path(__file__).parent / "rows"
ROWS = sorted(ROWS_DIR.glob("generated_*.yaml"))


def _declared_cells(document) -> list[str]:
    steps = (*document.get("given", ()), *document.get("when", ()))
    return [cell for step in steps if isinstance(step, dict) for cell in step.get("cells", ())]


@pytest.mark.parametrize("path", ROWS, ids=[path.stem.removeprefix("generated_") for path in ROWS])
def test_covering_row(lifecycle_world, path: Path) -> None:
    row, document = load_covering_row(path)
    for name in row.packages:
        assert set(lifecycle_world.packages[name].kinds) == set(WORLD_KINDS[name]), name
    replay = CoveringReplay(lifecycle_world, row, document)
    replay.replay()
    assert replay.credited == _declared_cells(document)
