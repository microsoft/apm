from __future__ import annotations

from pathlib import Path

import pytest

from tests.quality.repository_python_inventory import (
    PythonModuleFacts,
    tracked_python_inventory,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def repository_python_inventory() -> dict[str, PythonModuleFacts]:
    """Share one tracked Python AST inventory across quality contracts."""
    return tracked_python_inventory(REPO_ROOT)
