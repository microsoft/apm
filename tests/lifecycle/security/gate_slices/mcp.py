"""Gate removal rows for KIND mcp: p-exec's stdio MCP server entry (#4107).

The entry lives in a shared config file (``.mcp.json``, or
``~/.copilot/mcp-config.json`` at user scope), so the removal edits the file
instead of deleting it: the replay asserts the entry itself is gone, and the
obligation's snapshot fact is that the other scope keeps its bytes.
"""

from __future__ import annotations

from tests.lifecycle.security.gate_slices import GateRow, GateSlice

_PROJECT = ({"project": (".mcp.json",)}, {"home": (".copilot", ".claude")})
_USER = ({"home": (".copilot/mcp-config.json",)}, {"project": (".mcp.json", ".github")})
_ALL = (
    "definition-changed",
    "local-deny",
    "org-deny",
    "org-enables-gate",
    "policy-dep-deny",
    "revoke-approval",
)
_LOCAL = ("definition-changed", "local-deny", "revoke-approval")
_UPDATE_DEP_DENY = (
    "#0 apm update exits 0 and keeps the package deployed when the org policy denies a "
    "declared dependency; apm install refuses it"
)
_UNINSTALL_SIBLING = (
    "#0 apm uninstall of another package keeps an MCP server configured that the gate "
    "no longer allows; the next apm install removes it"
)
_DRY_RUN = (
    "#0 apm install --dry-run does not preview that an MCP server the gate no longer "
    "allows will be removed"
)
_XFAIL = {
    "G|update|policy-dep-deny|mcp": _UPDATE_DEP_DENY,
    **{f"G|uninstall-prune-rebuild|{change}|mcp": _UNINSTALL_SIBLING for change in _ALL},
    **{
        f"G|dry-run|{change}|mcp": _DRY_RUN
        for change in ("local-deny", "org-deny", "revoke-approval")
    },
}
_SMOKE = {"G|update|local-deny|mcp"}

# G path -> (changes with a row, (surface files, other-scope prefixes))
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
    kind="mcp",
    package="p-exec",
    target="copilot",
    rows=tuple(
        GateRow(
            f"G|{path}|{change}|mcp",
            surface=surface,
            shared=True,
            unchanged=unchanged,
            smoke=f"G|{path}|{change}|mcp" in _SMOKE,
            xfail=_XFAIL.get(f"G|{path}|{change}|mcp", ""),
        )
        for path, (changes, (surface, unchanged)) in _PATHS.items()
        for change in changes
    ),
)
