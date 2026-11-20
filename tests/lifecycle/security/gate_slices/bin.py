"""Gate removal rows for KIND bin: plugin bin/ executables (#4107).

bin/ deploys only for a marketplace Claude plugin at user scope
(install/exec_gate.py plugin_bin_deployable), so:

- user-scope marketplace rows replay p-native-claude-bin on claude and see
  ``~/.agents/plugins/p-native-claude-bin/bin/p-native-claude-bin`` go;
- every other path never deploys bin/: those rows replay p-exec on copilot
  and pin that its bin/ stays absent in both the project and HOME across
  the removing step, while the step names the gate decision.
"""

from __future__ import annotations

from tests.lifecycle.security.gate_slices import GateRow, GateSlice

_PLUGIN = "p-native-claude-bin"
_DEPLOYED = {"home": (f".agents/plugins/{_PLUGIN}/bin/{_PLUGIN}",)}
_NEVER = {
    "project": (".agents/plugins/p-exec/bin/p-exec",),
    "home": (".agents/plugins/p-exec/bin/p-exec",),
}
_NEVER_DEPLOYS = (
    "bin/ never deploys on this path (install/exec_gate.py plugin_bin_deployable; "
    "bundle/unpacker.py refuses executables): steward ruling pending"
)
_ALL = (
    "local-deny",
    "org-deny",
    "org-enables-gate",
    "policy-dep-deny",
    "revoke-approval",
)
_LOCAL = ("local-deny", "revoke-approval")
_DRY_RUN = "S-pending (neutral issue to follow): dry-run preview of gate removal"
_UNINSTALL_SIBLING = (
    "S-pending (neutral issue to follow): uninstall rebuild agreement on gated surfaces"
)
_UPDATE_DEP_DENY = "S-pending (neutral issue to follow): update and install agreement on a policy-denied dependency"
_BUNDLE = "S-pending (neutral issue to follow): reinstalling a local bundle keeps hook files the gate no longer allows"
_BIN_UNLOCKED = (
    "#4225 the user lockfile does not record the bin/ files a marketplace plugin deployed"
)
_XFAIL = {
    **{
        f"G|dry-run|{change}|bin": _DRY_RUN
        for change in ("local-deny", "org-deny", "revoke-approval")
    },
    **{f"G|uninstall-prune-rebuild|{change}|bin": _UNINSTALL_SIBLING for change in _ALL},
    "G|update|policy-dep-deny|bin": _UPDATE_DEP_DENY,
    **{
        f"G|bundle-install|{change}|bin": _BUNDLE
        for change in ("local-deny", "org-deny", "org-enables-gate", "revoke-approval")
    },
    **{f"G|marketplace-install|{change}|bin": _BIN_UNLOCKED for change in _ALL},
}

# G path -> changes with an absence-only row (p-exec never deploys bin/ there)
_ABSENT = {
    "install": _ALL,
    "frozen": _ALL,
    "update": _ALL,
    "uninstall-prune-rebuild": _ALL,
    "audit-ci": _ALL,
    "dry-run": ("local-deny", "org-deny", "revoke-approval"),
    "install-g": _LOCAL,
    "frozen-g": _LOCAL,
    "bundle-install": ("local-deny", "org-deny", "org-enables-gate", "revoke-approval"),
    "unpack": _ALL,
}
# G path -> changes with a real-deploy row (p-native-claude-bin, user scope, claude)
_REAL = {"marketplace-install": _ALL}


def _absent_row(path: str, change: str) -> GateRow:
    row_id = f"G|{path}|{change}|bin"
    return GateRow(
        row_id,
        surface=_NEVER,
        unchanged=_NEVER,
        package="p-exec",
        target="copilot",
        definition_pending=_NEVER_DEPLOYS,
        xfail=_XFAIL.get(row_id, ""),
    )


def _real_row(path: str, change: str) -> GateRow:
    row_id = f"G|{path}|{change}|bin"
    return GateRow(row_id, surface=_DEPLOYED, xfail=_XFAIL.get(row_id, ""))


SLICE = GateSlice(
    kind="bin",
    package=_PLUGIN,
    target="claude",
    rows=(
        *(_absent_row(path, change) for path, changes in _ABSENT.items() for change in changes),
        *(_real_row(path, change) for path, changes in _REAL.items() for change in changes),
    ),
)
