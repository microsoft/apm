"""Gate removal rows for KIND bin: plugin bin/ executables (#4107).

bin/ deploys only for a marketplace Claude plugin at user scope
(install/exec_gate.py plugin_bin_deployable), so:

- user-scope marketplace rows replay p-native-claude-bin on claude and see
  ``~/.agents/plugins/p-native-claude-bin/bin/p-native-claude-bin`` go;
- every other path never deploys bin/ (definition rule U-BIN, steward
  ruling; the T-5 generator lists those cells under ``excluded`` and pins the
  predicate). One witness row per path shape (project install, user-scope
  install, dry-run, bundle install, unpack) replays p-exec on copilot and
  pins that its bin/ stays absent in the project and HOME across the
  removing step, while the step names the gate decision.
"""

from __future__ import annotations

from tests.lifecycle.security.gate_slices import GateRow, GateSlice

_PLUGIN = "p-native-claude-bin"
_DEPLOYED = {"home": (f".agents/plugins/{_PLUGIN}/bin/{_PLUGIN}",)}
_NEVER = {
    "project": (".agents/plugins/p-exec/bin/p-exec",),
    "home": (".agents/plugins/p-exec/bin/p-exec",),
}
_ALL = (
    "local-deny",
    "org-deny",
    "org-enables-gate",
    "policy-dep-deny",
    "revoke-approval",
)
_DRY_RUN = "S-pending (neutral issue to follow): dry-run preview of gate removal"
_BUNDLE = "#4216 reinstalling a local bundle keeps hook files the gate no longer allows"
_BIN_UNLOCKED = (
    "#4225 the user lockfile does not record the bin/ files a marketplace plugin deployed"
)
_XFAIL = {
    "G|dry-run|local-deny|bin": _DRY_RUN,
    "G|bundle-install|local-deny|bin": _BUNDLE,
    **{f"G|marketplace-install|{change}|bin": _BIN_UNLOCKED for change in _ALL},
}

# U-BIN witness rows: one absence row per path shape where bin/ never deploys
# (install/exec_gate.py plugin_bin_deployable; bundle/unpacker.py refuses
# executables). The other absence cells are definition cells of U-BIN.
_ABSENT = {
    "install": ("local-deny",),
    "install-g": ("local-deny",),
    "dry-run": ("local-deny",),
    "bundle-install": ("local-deny",),
    "unpack": ("local-deny",),
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
