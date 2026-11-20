"""Gate removal rows for KIND lsp: p-lsp's language server entry (#4107).

The entry lives in a shared config file (``.github/lsp.json``, or
``~/.copilot/lsp-config.json`` at user scope); the replay asserts the entry
is gone, the obligation's snapshot fact is that removal stays in scope.
"""

from __future__ import annotations

from tests.lifecycle.security.gate_slices import GateRow, GateSlice

_PROJECT = {"home": (".copilot", ".claude")}
_USER = {"project": (".github",)}
_UNINSTALL_EXIT = (
    "#0 apm uninstall of another package removes a language server the gate no longer "
    "allows but exits 1 reporting incomplete integration cleanup"
)

SLICE = GateSlice(
    kind="lsp",
    package="p-lsp",
    target="copilot",
    rows=(
        GateRow("G|install|local-deny|lsp", unchanged=_PROJECT, smoke=True),
        GateRow("G|install|org-deny|lsp", unchanged=_PROJECT),
        GateRow("G|frozen|revoke-approval|lsp", unchanged=_PROJECT),
        GateRow("G|frozen|org-enables-gate|lsp", unchanged=_PROJECT),
        GateRow("G|update|org-enables-gate|lsp", unchanged=_PROJECT),
        GateRow("G|update|org-deny|lsp", unchanged=_PROJECT),
        GateRow("G|update|revoke-approval|lsp", unchanged=_PROJECT),
        GateRow(
            "G|uninstall-prune-rebuild|local-deny|lsp", unchanged=_PROJECT, xfail=_UNINSTALL_EXIT
        ),
        GateRow(
            "G|uninstall-prune-rebuild|org-deny|lsp", unchanged=_PROJECT, xfail=_UNINSTALL_EXIT
        ),
        GateRow("G|audit-ci|org-deny|lsp", unchanged=_PROJECT),
        GateRow("G|audit-ci|local-deny|lsp", unchanged=_PROJECT),
        GateRow("G|dry-run|revoke-approval|lsp", unchanged=_PROJECT),
        GateRow("G|install-g|revoke-approval|lsp", unchanged=_USER),
        GateRow("G|install-g|local-deny|lsp", unchanged=_USER),
        GateRow("G|frozen-g|local-deny|lsp", unchanged=_USER),
    ),
)
