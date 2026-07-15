"""Emit one repository-wide node and behavioral-marker inventory."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

BEHAVIORAL_MARKERS = frozenset({"unit", "component", "e2e"})
OUTPUT_ENV = "APM_TAXONOMY_INVENTORY"


def pytest_collection_finish(session: pytest.Session) -> None:
    """Write the collected node IDs and their behavioral markers once."""
    output = os.environ.get(OUTPUT_ENV)
    if output is None:
        raise pytest.UsageError(f"{OUTPUT_ENV} must name the inventory output")
    inventory = {
        item.nodeid: sorted(
            marker.name for marker in item.iter_markers() if marker.name in BEHAVIORAL_MARKERS
        )
        for item in session.items
    }
    Path(output).write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
