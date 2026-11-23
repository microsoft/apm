"""A local bundle reinstalled after the lockfile is lost leaves a state both gates accept (#4634).

PROMISE: P-INV-I3 -- ``install --frozen`` and ``audit --ci`` agree after a
successful install (source: tests/lifecycle/oracles.py).
PROMISE: P-LAW-outcome.status_matches_state -- an install that exits 0 leaves a
lockfile the gates accept (source: tests/fixtures/lifecycle_bug_ledger.json).
PROMISE: P-LAW-ownership.preserve_unowned -- an identical entry APM does not
manage stays the user's: equality never establishes ownership
(source: src/apm_cli/install/mcp/ownership.py NativeMcpConnectionState).
CELL: OP=bundle-install SRC=local-bundle PRIM=mcp DEPLOYED=present-provenance-unknown.
OWNER: src/apm_cli/install/mcp/integration.py ``run_owned_mcp_integration`` --
records a bundle's MCP server only where this install claims it, which is what
``mcp_config_view._bundle_owned_baseline`` reads back.

The world's ``p-exec`` bundle ships a hook and the stdio server ``p-exec-mcp``.
After an approved install, ``apm.lock.yaml`` is deleted and the bundle is
reinstalled: the deployed hook and ``.mcp.json`` entry now have no recorded
owner. Every step runs I1-I9 through ``StepRunner.step``; the printed hint
(remove the entry, reinstall the bundle) is executed.
"""

from __future__ import annotations

import json

import pytest

from tests.lifecycle.machines._kit import Bench, StepRunner
from tests.lifecycle.model.state import ModelState

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-INV-I3",
        "P-LAW-outcome.status_matches_state",
        "P-LAW-ownership.preserve_unowned",
    ),
]

SERVER = "p-exec-mcp"
_PENDING = "apm_modules/.local-bundle-pending.json"
_SKIPPED = (
    f"Skipping MCP server '{SERVER}' from bundle p-exec -- an entry with that name exists "
    "that APM does not manage. Remove the entry, then reinstall the bundle to manage it."
)


def _servers(bench: Bench) -> dict:
    path = bench.deploy_root / ".mcp.json"
    return json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}


def _lock_text(bench: Bench) -> str:
    return bench.lock_path.read_text(encoding="utf-8")


def _gates_accept(runner: StepRunner) -> None:
    """``audit --ci`` and ``install --frozen`` both accept the state (I3)."""
    runner.step(("audit", "--ci"), exits={0}, read_only=True)
    runner.step(("install", "--frozen"), exits={0})


def test_bundle_reinstall_after_lost_lock_leaves_a_state_the_gates_accept(
    lifecycle_world,
) -> None:
    bench = Bench(lifecycle_world, "project")
    runner = StepRunner(bench, ModelState(), {})
    bundle = lifecycle_world.packages["p-exec:bundle"].spec

    # An approved install deploys the hook and records the bundle's server.
    runner.step(("install", bundle), exits={0})
    pending = json.loads((bench.deploy_root / _PENDING).read_text(encoding="utf-8"))
    runner.step(("approve", pending["bundles"]["p-exec"]["approval_key"]), exits={0})
    runner.step(("install", bundle), exits={0})
    assert SERVER in _servers(bench)
    assert f"{SERVER}: bundle:p-exec" in _lock_text(bench)
    _gates_accept(runner)

    # The lockfile is lost: the deployed entry has no recorded owner any more.
    runner.edit("lose-lock", bench.lock_path.unlink)
    entry = _servers(bench)[SERVER]

    # Decisive: the reinstall leaves the unmanaged entry alone, records no
    # claim it cannot back with a deployment, and says what to do.
    step = runner.step(("install", bundle), exits={0})
    _gates_accept(runner)
    assert _servers(bench)[SERVER] == entry
    assert SERVER not in _lock_text(bench)
    output = step.result.stdout + step.result.stderr
    assert _SKIPPED in " ".join(output.split()), output

    # The printed hint: remove the entry, reinstall the bundle -> APM owns it.
    def drop_entry() -> None:
        path = bench.deploy_root / ".mcp.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["mcpServers"][SERVER]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    runner.edit("drop-unmanaged-entry", drop_entry)
    step = runner.step(("install", bundle), exits={0})
    assert "Skipping MCP server" not in step.result.stdout + step.result.stderr
    assert _servers(bench)[SERVER] == entry
    assert f"{SERVER}: bundle:p-exec" in _lock_text(bench)
    _gates_accept(runner)
