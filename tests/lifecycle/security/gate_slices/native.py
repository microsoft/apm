"""Gate removal rows for KIND native: p-native-claude's plugin MCP server (#4107).

The Claude plugin's stdio server lands in the project ``.mcp.json`` (claude
target), shared with other servers; the entry goes when the gate closes.
"""

from __future__ import annotations

from tests.lifecycle.known import KnownFailure
from tests.lifecycle.security.gate_slices import GateRow, GateSlice

_PROJECT = {"project": (".mcp.json",)}
_ALL = (
    "definition-changed",
    "local-deny",
    "org-deny",
    "org-enables-gate",
    "policy-dep-deny",
    "revoke-approval",
)
# Plain files of the plugin survive an executable-level gate change.
_SKILL = {"project": (".claude/skills/p-native-claude",)}
# gate_paths/native_plugin.py: the git-served p-native-claude-bin at user scope; the
# approved plugin lands whole under ~/.agents/plugins and goes as one unit.
_PLUGIN = {"home": (".agents/plugins/p-native-claude-bin/.claude-plugin/plugin.json",)}
# definition-changed needs a new published revision of the git package: residual.
_PLUGIN_CHANGES = (
    "local-deny",
    "org-deny",
    "org-enables-gate",
    "policy-dep-deny",
    "revoke-approval",
)
_USER_ORG = KnownFailure(
    "#3703",
    "user-scope installs discover no org policy, so an org policy change leaves"
    " user-scope deployments in place",
    # I4 keeps the surface (org-deny, org-enables-gate); the dep deny row keeps
    # the plugin file and prints no deny (policy-dep-deny).
    r"still deployed but not allowed|plugins/p-native-claude-bin/\S+ must be absent"
    r"|output lacks /denied by pattern/",
)
_XFAIL = {
    **{
        f"G|native-plugin|{change}|native": _USER_ORG
        for change in ("org-deny", "org-enables-gate", "policy-dep-deny")
    },
}
_SMOKE = {"G|install|org-enables-gate|native"}

# G path -> (changes with a row, surface files)
_PATHS = {
    "install": (_ALL, _PROJECT),
    "frozen": (_ALL, _PROJECT),
    "update": (_ALL, _PROJECT),
    "uninstall-prune-rebuild": (_ALL, _PROJECT),
    "audit-ci": (_ALL, _PROJECT),
}

SLICE = GateSlice(
    kind="native",
    package="p-native-claude",
    target="claude",
    rows=tuple(
        GateRow(
            f"G|{path}|{change}|native",
            surface=surface,
            shared=True,
            unchanged=_SKILL,
            smoke=f"G|{path}|{change}|native" in _SMOKE,
            xfail=_XFAIL.get(f"G|{path}|{change}|native"),
        )
        for path, (changes, surface) in _PATHS.items()
        for change in changes
    )
    + tuple(
        GateRow(
            f"G|native-plugin|{change}|native",
            surface=_PLUGIN,
            package="p-native-claude-bin",
            xfail=_XFAIL.get(f"G|native-plugin|{change}|native"),
        )
        for change in _PLUGIN_CHANGES
    ),
)
