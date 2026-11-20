"""Gate removal rows for KIND hook: p-exec's SessionStart hook file (#4107)."""

from __future__ import annotations

from tests.lifecycle.security.gate_slices import GateRow, GateSlice

_PROJECT = {"project": (".github/hooks/p-exec-session.json",)}
_USER = {"home": (".copilot/hooks/p-exec-session.json",)}
_ALL = (
    "definition-changed",
    "local-deny",
    "org-deny",
    "org-enables-gate",
    "policy-dep-deny",
    "revoke-approval",
)
_LOCAL = ("definition-changed", "local-deny", "revoke-approval")
_UPDATE_REDEFINED = "S-pending (neutral issue to follow): update and install agreement on gated surfaces after a definition change"
_UPDATE_DEP_DENY = "S-pending (neutral issue to follow): update and install agreement on a policy-denied dependency"
_UNINSTALL_SIBLING = (
    "S-pending (neutral issue to follow): uninstall rebuild agreement on gated surfaces"
)
_DRY_RUN = "S-pending (neutral issue to follow): dry-run preview of gate removal"
_XFAIL = {
    "G|update|definition-changed|hook": _UPDATE_REDEFINED,
    "G|update|policy-dep-deny|hook": _UPDATE_DEP_DENY,
    **{f"G|uninstall-prune-rebuild|{change}|hook": _UNINSTALL_SIBLING for change in _ALL},
    **{f"G|dry-run|{change}|hook": _DRY_RUN for change in ("local-deny", "org-deny")},
    "G|dry-run|revoke-approval|hook": _DRY_RUN,
}
_SMOKE = {"G|install|revoke-approval|hook", "G|frozen|local-deny|hook"}

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
    kind="hook",
    package="p-exec",
    target="copilot",
    rows=tuple(
        GateRow(
            f"G|{path}|{change}|hook",
            surface=surface,
            smoke=f"G|{path}|{change}|hook" in _SMOKE,
            xfail=_XFAIL.get(f"G|{path}|{change}|hook", ""),
        )
        for path, (changes, surface) in _PATHS.items()
        for change in changes
    ),
)
