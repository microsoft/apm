"""Session hook of the lifecycle state machines (#4031, #4095)."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

_GATE_KIT = "tests.lifecycle.machines._gate_kit"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Write the GATE cells this process visited for the coverage check.

    Each xdist worker writes its own ``visited-<worker>.json`` under
    ``build/gate-cells/`` (or ``$APM_GATE_CELLS_DIR``); the quality check reads
    their union.
    """
    del exitstatus
    kit = sys.modules.get(_GATE_KIT)
    if kit is None or not kit.VISITED_CELLS:
        return
    directory = Path(
        os.environ.get("APM_GATE_CELLS_DIR") or session.config.rootpath / "build" / "gate-cells"
    )
    directory.mkdir(parents=True, exist_ok=True)
    # Unique per process: shard artifacts merge into one directory.
    worker = f"{os.environ.get('PYTEST_XDIST_WORKER', 'main')}-{uuid.uuid4().hex[:8]}"
    visited = {"cells": sorted(kit.VISITED_CELLS), "spines": sorted(kit.COMPLETED_SPINES)}
    (directory / f"visited-{worker}.json").write_text(
        json.dumps(visited, indent=1) + "\n", encoding="ascii"
    )
