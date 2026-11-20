"""T-0 example row through the real CLI: earns I1-I9 receipts (#4093).

The row replays through ``tests/lifecycle/rows.py``: the driver runs every
command, ``check_cross_cutting`` checks every step, and the final world must
equal ``expected_deployed`` of the reference model. A recorded mutant that
skips the approval check must fail the same row.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.lifecycle.rows import RowReplay, load_row

pytestmark = [pytest.mark.integration, pytest.mark.lifecycle_smoke]

ROW = Path(__file__).parent / "replay" / "rows" / "core_gate_row.yaml"
MUTANT = Path(__file__).parents[1] / "fixtures" / "gate_mutants" / "skip_approval_check.py"


def test_core_gate_row_keeps_every_invariant(lifecycle_world) -> None:
    RowReplay(lifecycle_world, load_row(ROW)).replay()


def test_core_gate_row_kills_the_recorded_approval_mutant(lifecycle_world) -> None:
    replay = RowReplay(lifecycle_world, load_row(ROW), entry_point=(sys.executable, str(MUTANT)))
    with pytest.raises(AssertionError, match=r"I4 .*p-exec/hook/copilot still deployed"):
        replay.replay()
