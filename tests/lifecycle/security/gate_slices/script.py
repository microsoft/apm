"""Gate removal rows for KIND script: a trusted project lifecycle script (#4107).

The script appends to ``script-runs.log`` when it runs; after the gate
change the next install must not run it, so the log keeps its bytes.
"""

from __future__ import annotations

from tests.lifecycle.security.gate_slices import GateRow, GateSlice

_LOG = {"project": ("script-runs.log",)}

SLICE = GateSlice(
    kind="script",
    package="p-skill",
    target="copilot",
    rows=(
        GateRow(
            "G|lifecycle-scripts|org-deny|script",
            shared=True,
            unchanged=_LOG,
            announce=r"Lifecycle scripts suppressed by org policy \(executables\.deny_all\)",
        ),
        GateRow(
            "G|lifecycle-scripts|revoke-approval|script",
            shared=True,
            unchanged=_LOG,
            announce=r"Skipped \d+ untrusted project script",
            smoke=True,
        ),
    ),
)
