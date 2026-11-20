"""Gate removal rows for KIND mcp: p-exec's stdio MCP server entry (#4107).

The entry lives in a shared config file (``.mcp.json``, or
``~/.copilot/mcp-config.json`` at user scope) that other servers may share,
so the replay asserts the entry itself is gone; the obligation's snapshot
fact is that the removal stays inside its scope.
"""

from __future__ import annotations

from tests.lifecycle.security.gate_slices import GateRow, GateSlice

_PROJECT = {"home": (".copilot", ".claude")}
_USER = {"project": (".mcp.json", ".github")}
_ALL = (
    "definition-changed",
    "local-deny",
    "org-deny",
    "org-enables-gate",
    "policy-dep-deny",
    "revoke-approval",
)
_UPDATE_DEP_DENY = (
    "#0 apm update exits 0 and keeps the package deployed when the org policy denies a "
    "declared dependency; apm install refuses it"
)
_UNINSTALL_SIBLING = (
    "#0 apm uninstall of another package keeps an MCP server configured that the gate "
    "no longer allows; the next apm install removes it"
)
_XFAIL = {
    "G|update|policy-dep-deny|mcp": _UPDATE_DEP_DENY,
    **{f"G|uninstall-prune-rebuild|{change}|mcp": _UNINSTALL_SIBLING for change in _ALL},
}
_SMOKE = {"G|update|local-deny|mcp"}

_PATHS = {
    "install": (_ALL, _PROJECT),
    "frozen": (_ALL, _PROJECT),
    "update": (_ALL, _PROJECT),
    "uninstall-prune-rebuild": (_ALL, _PROJECT),
    "audit-ci": (_ALL, _PROJECT),
    "dry-run": (("local-deny", "org-deny", "revoke-approval"), _PROJECT),
    "install-g": (("definition-changed", "local-deny", "revoke-approval"), _USER),
    "frozen-g": (("definition-changed", "local-deny", "revoke-approval"), _USER),
    "bundle-install": (
        ("local-deny", "org-deny", "org-enables-gate", "revoke-approval"),
        _PROJECT,
    ),
    "marketplace-install": (
        ("local-deny", "org-deny", "org-enables-gate", "policy-dep-deny", "revoke-approval"),
        _PROJECT,
    ),
}

SLICE = GateSlice(
    kind="mcp",
    package="p-exec",
    target="copilot",
    rows=tuple(
        GateRow(
            f"G|{path}|{change}|mcp",
            unchanged=unchanged,
            smoke=f"G|{path}|{change}|mcp" in _SMOKE,
            xfail=_XFAIL.get(f"G|{path}|{change}|mcp", ""),
        )
        for path, (changes, unchanged) in _PATHS.items()
        for change in changes
    ),
)
