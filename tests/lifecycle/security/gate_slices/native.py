"""Gate removal rows for KIND native: p-native-claude's MCP server (#4107).

The plugin's skill is ungated; its MCP server, configured for Claude in the
project's ``.mcp.json``, is the gated surface. Rows that need an approval
first are strict expected failures: approve cannot find the parked plugin.
"""

from __future__ import annotations

from tests.lifecycle.security.gate_slices import GateRow, GateSlice

_PROJECT = {"home": (".copilot", ".claude")}
_APPROVE = "#4154 apm approve cannot find a native plugin parked by the executables gate"

SLICE = GateSlice(
    kind="native",
    package="p-native-claude",
    target="claude",
    rows=(
        GateRow("G|install|org-enables-gate|native", unchanged=_PROJECT),
        GateRow("G|update|org-enables-gate|native", unchanged=_PROJECT),
        GateRow("G|audit-ci|org-enables-gate|native", unchanged=_PROJECT),
        GateRow("G|install|local-deny|native", unchanged=_PROJECT, xfail=_APPROVE),
        GateRow("G|frozen|revoke-approval|native", unchanged=_PROJECT, xfail=_APPROVE),
        GateRow("G|update|definition-changed|native", unchanged=_PROJECT, xfail=_APPROVE),
    ),
)
