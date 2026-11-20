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
_BUNDLE = tuple(change for change in _ALL if change != "policy-dep-deny")
_UPDATE_DEP_DENY = "S-pending (neutral issue to follow): update and install agreement on a policy-denied dependency"
_UNINSTALL_SIBLING = (
    "S-pending (neutral issue to follow): uninstall rebuild agreement on gated surfaces"
)
_DRY_RUN = "S-pending (neutral issue to follow): dry-run preview of gate removal"
_XFAIL = {
    "G|update|policy-dep-deny|mcp": _UPDATE_DEP_DENY,
    **{f"G|uninstall-prune-rebuild|{change}|mcp": _UNINSTALL_SIBLING for change in _ALL},
    **{
        f"G|dry-run|{change}|mcp": _DRY_RUN
        for change in ("local-deny", "org-deny", "org-enables-gate", "revoke-approval")
    },
}
_BUNDLE_KEEPS = "S-pending (neutral issue to follow): reinstalling a local bundle keeps hook files the gate no longer allows"
_XFAIL.update(
    {
        f"G|bundle-install|{change}|mcp": _BUNDLE_KEEPS
        for change in ("local-deny", "org-deny", "org-enables-gate", "revoke-approval")
    }
)
# A bundle approval pins the whole bundle digest: a changed definition reads as
# a bundle that is not approved yet.
_ANNOUNCE = {"G|bundle-install|definition-changed|mcp": r"not approved for this exact bundle"}
_SMOKE = {"G|update|local-deny|mcp"}

# G path -> (changes with a row, (surface files, other-scope prefixes))
_MARKETPLACE = tuple(change for change in _ALL if change != "definition-changed")
_DENY_BY_SPEC = "S-pending (neutral issue to follow): deny by install spec and installed identity"
_XFAIL["G|marketplace-install|local-deny|mcp"] = _DENY_BY_SPEC
_USER_ORG = "S-pending (neutral issue to follow): org policy for user-scope installs"
_XFAIL.update(
    {
        f"G|marketplace-install|{change}|mcp": _USER_ORG
        for change in ("org-deny", "org-enables-gate", "policy-dep-deny")
    }
)

_PATHS = {
    "install": (_ALL, _PROJECT),
    "frozen": (_ALL, _PROJECT),
    "update": (_ALL, _PROJECT),
    "uninstall-prune-rebuild": (_ALL, _PROJECT),
    "audit-ci": (_ALL, _PROJECT),
    "dry-run": (("local-deny", "org-deny", "revoke-approval"), _PROJECT),
    "install-g": (_LOCAL, _USER),
    "frozen-g": (_LOCAL, _USER),
    # gate_paths/bundle_install.py: the MCP server of the local bundle p-exec:bundle.
    "bundle-install": (_BUNDLE, _PROJECT),
    # gate_paths/marketplace_install.py: p-exec@lifecycle-market at user scope. A
    # git-served package cannot be redefined in place: definition-changed is residual.
    "marketplace-install": (_MARKETPLACE, _USER),
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
            announce=_ANNOUNCE.get(f"G|{path}|{change}|mcp"),
            unchanged=unchanged,
            smoke=f"G|{path}|{change}|mcp" in _SMOKE,
            xfail=_XFAIL.get(f"G|{path}|{change}|mcp", ""),
        )
        for path, (changes, (surface, unchanged)) in _PATHS.items()
        for change in changes
    ),
)
