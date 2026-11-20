"""Wiring of the lifecycle test family (#4093).

- driver selection: ``lifecycle_entry_point`` (``APM_LIFECYCLE_DRIVER``);
- world fixtures: ``tests/lifecycle/world`` (lane T-0W) when present, else
  the fallback world;
- receipts plugin: ``ReceiptWitnessPlugin`` (session receipts directory and
  the per-witness obligation ratchet);
- global output oracle: every driver command keeps I6, I8 and I9.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.lifecycle import driver, oracles
from tests.lifecycle.obligations import ReceiptWitnessPlugin, load_catalog
from tests.utils.apm_lifecycle_runner import CommandResult

if (Path(__file__).parent / "world" / "fixtures.py").is_file():
    from tests.lifecycle.world.fixtures import lifecycle_template, lifecycle_world
else:
    from tests.lifecycle._fallback_world import lifecycle_template, lifecycle_world

__all__ = ["lifecycle_template", "lifecycle_world"]

_PLUGIN_NAME = "lifecycle-receipts"


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
