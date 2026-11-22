"""Commands over an installed local bundle (#4096).

PROMISE: P-INV-I1 -- ``compile`` and ``lock`` over a project with an approved
local bundle keep its deployed files and its lock records (a bundle has no
dependency row: its deployment and MCP provenance carry it).
PROMISE: P-INV-I5 -- neither command writes outside its declared write set.

T-2 pairs OP x SRC=local-bundle. Credits name only the two factors, so the
OP x SRC x PRIM triples stay with the source spine.
"""

from __future__ import annotations

import re

import pytest

from tests.lifecycle.scenarios._kit import Scenario, assert_changes, credits
from tests.lifecycle.world import use_policy

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I1", "P-INV-I5"),
]

# The bundle packs p-exec, so its files carry that package name.
_BUNDLE_KINDS = {"p-exec": ("hook", "mcp", "bin")}
_BUNDLE_OPS = {"compile": "compile", "lock": "lock"}


@pytest.mark.parametrize("op", sorted(_BUNDLE_OPS))
def test_compile_and_lock_keep_an_installed_local_bundle(lifecycle_world, op) -> None:
    use_policy(lifecycle_world, "allow")
    specs = {"p-exec": lifecycle_world.packages["p-exec:bundle"].spec}
    scenario = Scenario(lifecycle_world, specs, policy="allow", kinds=_BUNDLE_KINDS)
    first = scenario.run("install p-exec")
    hint = re.search(r"apm approve '([^']+)'", first.output)
    assert hint, first.output
    approved = scenario.model.approve("p-exec", _BUNDLE_KINDS["p-exec"])
    scenario.run(("approve", hint.group(1)), requested=approved)
    scenario.run("install p-exec")
    lock_before = scenario.lock_path.read_bytes()
    step = scenario.run(_BUNDLE_OPS[op], credit=[{"OP": op, "SRC": "local-bundle"}])
    assert step.result.returncode == 0, step.output
    scenario.assert_deployed()
    assert_changes(
        [item for item in step.changed if not item.endswith("apm.lock.yaml")],
        (),
        label=op,
    )
    # A local bundle has no dependency row: its deployment and MCP provenance carry it.
    lock_text = scenario.lock_path.read_text(encoding="utf-8")
    assert "active_owner: local-bundle" in lock_text, lock_text
    assert "p-exec-mcp: bundle:p-exec" in lock_text, lock_text
    if op == "compile":
        assert scenario.lock_path.read_bytes() == lock_before, "compile never rewrites the lock"
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        f"test_compile_and_lock_keep_an_installed_local_bundle[{op}]": [
            {"OP": op, "SRC": "local-bundle"}
        ]
        for op in _BUNDLE_OPS
    },
)
