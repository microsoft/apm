"""Gate removal rows for KIND lsp: p-lsp's LSP server entry (#4107).

The server lands in ``.github/lsp.json`` (project) or ``.copilot/lsp-config.json``
(user); with one server the file goes when the entry goes.
"""

from __future__ import annotations

from tests.lifecycle.security.gate_slices import GateRow, GateSlice

_PROJECT = {"project": (".github/lsp.json",)}
_USER = {"home": (".copilot/lsp-config.json",)}
_ALL = (
    "local-deny",
    "org-deny",
    "org-enables-gate",
    "policy-dep-deny",
    "revoke-approval",
)
_LOCAL = ("local-deny", "revoke-approval")
_STALE_EXEC_STATUS = (
    "#4053 apm install --frozen accepts a lock whose exec_status is out of date after a gate change"
)
_DRY_RUN = "S-pending (neutral issue to follow): dry-run preview of gate removal"
_UNINSTALL_SIBLING = (
    "#4215 apm uninstall of another package exits 1 and reports incomplete integration "
    "cleanup after removing an LSP server the gate no longer allows"
)
_UPDATE_DEP_DENY = "S-pending (neutral issue to follow): update and install agreement on a policy-denied dependency"
_XFAIL = {
    **{
        f"G|{path}|{change}|lsp": _STALE_EXEC_STATUS
        for path in ("frozen", "audit-ci")
        for change in _ALL
        if change != "policy-dep-deny"
    },
    **{f"G|frozen-g|{change}|lsp": _STALE_EXEC_STATUS for change in _LOCAL},
    **{
        f"G|dry-run|{change}|lsp": _DRY_RUN
        for change in ("local-deny", "org-deny", "revoke-approval")
    },
    **{f"G|uninstall-prune-rebuild|{change}|lsp": _UNINSTALL_SIBLING for change in _ALL},
    "G|update|policy-dep-deny|lsp": _UPDATE_DEP_DENY,
}
_SMOKE = {"G|install|local-deny|lsp"}

# G path -> (changes with a row, surface files)
_PATHS = {
    "install": (_ALL, _PROJECT),
    "frozen": (_ALL, _PROJECT),
    "update": (_ALL, _PROJECT),
    "uninstall-prune-rebuild": (_ALL, _PROJECT),
    "audit-ci": (_ALL, _PROJECT),
    "dry-run": (("local-deny", "org-deny", "revoke-approval"), _PROJECT),
    "install-g": (_LOCAL, _USER),
    "frozen-g": (_LOCAL, _USER),
}

SLICE = GateSlice(
    kind="lsp",
    package="p-lsp",
    target="copilot",
    rows=tuple(
        GateRow(
            f"G|{path}|{change}|lsp",
            surface=surface,
            smoke=f"G|{path}|{change}|lsp" in _SMOKE,
            xfail=_XFAIL.get(f"G|{path}|{change}|lsp", ""),
        )
        for path, (changes, surface) in _PATHS.items()
        for change in changes
    ),
)
