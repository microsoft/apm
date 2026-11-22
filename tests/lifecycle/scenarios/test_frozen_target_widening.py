"""A refused ``--frozen`` install on a widened target set changes no file (#4638).

PROMISE: P-INV-I7 / P-LAW-transaction.failed_command_preserves_state -- when
apm.yml declares a target the lockfile does not record, ``apm install --frozen``
refuses, and the install transaction rolls back every path it deployed,
merged hook configuration (``.claude/settings.json``, ``apm-hooks.json`` and
the ``.gemini/`` pair) included (source: src/apm_cli/install/transaction.py).
A plain install afterwards deploys and records the new target.

CELL: OP=install SRC=local-path PRIM=hook SCOPE=project TGT=widened FROZEN=on
"""

from __future__ import annotations

import re

import pytest

from tests.lifecycle.scenarios._kit import Scenario, assert_changes
from tests.lifecycle.world import use_policy

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I7", "P-LAW-transaction.failed_command_preserves_state"),
]

# The lock records neither the added target's files nor its native (MCP/LSP)
# ownership; whichever check runs first refuses.
_REFUSAL = re.compile(
    r"--frozen: apm\.lock\.yaml does not record everything this install deploys\."
    r"|--frozen cannot replay the selected native services\."
)
_PACKAGES = ("p-skill:git", "p-child", "p-exec")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (("copilot",), ("copilot", "claude")),
        (("copilot", "claude"), ("copilot", "claude", "gemini")),
    ],
    ids=["single-widened", "multi-widened"],
)
def test_a_refused_frozen_widening_leaves_no_hook_config(
    lifecycle_world, before: tuple[str, ...], after: tuple[str, ...]
) -> None:
    use_policy(lifecycle_world, "allow")
    scenario = Scenario(lifecycle_world, _PACKAGES, targets=before, policy="allow")
    scenario.run(f"install {' '.join(_PACKAGES)}")
    scenario.run("approve p-exec")
    scenario.run("install")
    scenario.set_targets(*after)

    refused = scenario.run("install --frozen", exit=1)

    assert _REFUSAL.search(" ".join(refused.output.split())), refused.output
    assert_changes(refused.changed, (), label="refused frozen widening")

    scenario.run("install")
    scenario.assert_deployed()
    scenario.finish()
