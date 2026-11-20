"""Wiring of the lifecycle test family (#4093).

- driver selection: ``lifecycle_entry_point`` (``APM_LIFECYCLE_DRIVER``);
- world fixtures: ``tests/lifecycle/world`` (lane T-0W);
- receipts plugin: ``ReceiptWitnessPlugin`` (session receipts directory and
  the per-witness obligation ratchet);
- global output oracle: every driver command keeps I6, I8 and I9.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.lifecycle import driver, oracles
from tests.lifecycle.obligations import ReceiptWitnessPlugin, load_catalog
from tests.lifecycle.world.fixtures import lifecycle_template, lifecycle_world
from tests.quality.taxonomy import directory_marker
from tests.quality.taxonomy_inventory_plugin import BEHAVIORAL_MARKERS
from tests.utils.apm_lifecycle_runner import CommandResult

__all__ = ["lifecycle_template", "lifecycle_world"]

_PLUGIN_NAME = "lifecycle-receipts"


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply the behavioral marker a lifecycle family directory determines (tm006)."""
    root = Path(__file__).resolve().parents[2]
    for item in items:
        path = Path(str(item.fspath)).resolve()
        if not path.is_relative_to(root):
            continue
        implied = directory_marker(path.relative_to(root).as_posix())
        if implied and not any(item.get_closest_marker(name) for name in BEHAVIORAL_MARKERS):
            item.add_marker(implied)


def pytest_configure(config: pytest.Config) -> None:
    if not config.pluginmanager.has_plugin(_PLUGIN_NAME):
        config.pluginmanager.register(ReceiptWitnessPlugin(load_catalog()), _PLUGIN_NAME)


@pytest.fixture(scope="session")
def lifecycle_entry_point() -> tuple[str, ...]:
    """The APM entry point the selected driver runs."""
    return driver.driver_command()


@pytest.fixture(autouse=True)
def lifecycle_output_oracle():
    """Check I6, I8 and I9 on every command a lifecycle test runs through the driver."""

    def hook(args: tuple[str, ...], result: CommandResult, scope: str) -> None:
        oracles.check_output(result, oracles.operation_for(args, scope=scope))

    driver.OUTPUT_HOOKS.append(hook)
    yield
    driver.OUTPUT_HOOKS.remove(hook)
