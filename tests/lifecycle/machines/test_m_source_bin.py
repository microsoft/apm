"""M-SOURCE bin spine: a whole plugin's bin/ through every resolving command (#4096).

See tests/lifecycle/machines/_source_bin.py for the promise and the cells.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.lifecycle.machines._gate_plugin_bin import ROOT, PluginBinBench
from tests.lifecycle.machines._kit import Bench, WorldPool, earned_cells
from tests.lifecycle.machines._source_bin import SourceBin, replay_source_bin, source_bin_cells
from tests.lifecycle.machines._source_bundle import (
    SourceBundle,
    replay_source_bundle,
    source_bundle_cells,
)
from tests.lifecycle.machines._source_kit import (
    KNOWN_IDS,
    SourceKit,
    replay_source_kit,
    source_kit_cells,
)
from tests.lifecycle.receipts import current_node

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I1", "P-INV-I4", "P-INV-I5", "P-INV-I7"),
]


def test_source_bin_spine_credits_op_src_bin_cells(lifecycle_template, tmp_path: Path) -> None:
    """bin/ of a whole plugin declared by each form keeps I1-I9 through every command."""
    world = WorldPool(lifecycle_template, tmp_path).fresh()
    machine = SourceBin(PluginBinBench(world, "project"))
    replay_source_bin(machine)
    missing = source_bin_cells() - earned_cells(current_node(), "SB|")
    assert not missing, f"source bin ids without an oracle receipt: {sorted(missing)}"
    # Each form ends with an orphan prune: the plugin root is gone.
    assert not (machine.bench.deploy_root / ROOT).exists()


def test_source_kit_spine_credits_registry_executable_cells(
    lifecycle_template, tmp_path: Path
) -> None:
    """A registry package's hook, LSP and canvas keep I1-I9 through every command."""
    world = WorldPool(lifecycle_template, tmp_path).fresh()
    machine = SourceKit(Bench(world, "project"))
    replay_source_kit(machine)
    missing = source_kit_cells() - set(KNOWN_IDS) - earned_cells(current_node(), "SK|")
    assert not missing, f"source kit ids without an oracle receipt: {sorted(missing)}"
    strict = {cell.label for cell in machine.known}
    assert machine.expected_seen == strict, sorted(strict - machine.expected_seen)
    # The spine ends with an orphan prune: the package's canvas extension is gone.
    assert not (machine.bench.deploy_root / ".github" / "extensions" / "p-kit").exists()


def test_source_bundle_spine_credits_bundle_executable_cells(
    lifecycle_template, tmp_path: Path
) -> None:
    """A local bundle's hook, LSP and canvas keep I1-I9 through lock, compile, audit and approve."""
    world = WorldPool(lifecycle_template, tmp_path).fresh()
    machine = SourceBundle(Bench(world, "project"))
    replay_source_bundle(machine)
    missing = source_bundle_cells() - earned_cells(current_node(), "SB2|")
    assert not missing, f"source bundle ids without an oracle receipt: {sorted(missing)}"
    # The spine ends with the approved reinstall: the bundle's canvas extension is deployed.
    assert (machine.bench.deploy_root / ".github" / "extensions" / "p-pack").is_dir()
