"""Gate removal rows for KIND script: project lifecycle scripts (#4107).

A trusted ``lifecycle:`` post-install script appends to
``script-runs.log`` in the project; after the change the next install must
run nothing, so the log keeps its bytes.
"""

from __future__ import annotations

from tests.lifecycle.security.gate_slices import GateRow, GateSlice

_RUNS = {"project": ("script-runs.log",)}

SLICE = GateSlice(
    kind="script",
    package="p-skill",
    target="copilot",
    rows=(
        GateRow(
            "G|lifecycle-scripts|revoke-approval|script",
            unchanged=_RUNS,
            announce=r"",
        ),
        GateRow(
            "G|lifecycle-scripts|org-deny|script",
            unchanged=_RUNS,
            announce=r"",
        ),
    ),
)
